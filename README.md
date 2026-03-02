# autr-back

Auto-trading backend refactored into a layered `src/autr` architecture.

## Run API

```bash
pip install -e .
cp .env.example .env
./scripts/run_api.sh
```

## Key Paths

- `src/autr/api`: FastAPI app and routes
- `src/autr/domain`: domain rules and state machine
- `src/autr/infra`: exchange and storage adapters
- `src/autr/services`: collector/strategy/executor units
- `src/autr/ops`: startup, logging, heartbeat

---

## API Endpoints

Base URL: `http://localhost:8000`

모든 인증이 필요한 엔드포인트에는 헤더에 `X-API-KEY: {ADMIN_KEY}` 가 필요합니다.

---

### System

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | ❌ | 서버 상태 확인 |
| `GET` | `/health` | ❌ | 헬스체크 (heartbeat 포함) |
| `GET` | `/ready` | ❌ | Ready 상태 확인 |
| `GET` | `/status` | ❌ | 현재 포지션 및 최근 주문 조회 |

---

### Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/auth/validate` | ✅ | API 키 유효성 검증 |

---

### Trading Control

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/trading/start` | ✅ | 자동매매 활성화 |
| `POST` | `/api/trading/stop` | ✅ | 자동매매 비활성화 |
| `GET` | `/api/trading/status` | ❌ | 자동매매 현재 상태 조회 (Consumer Redis 상태) |
| `POST` | `/api/trading/params` | ✅ | 전략 파라미터 업데이트 |
| `GET` | `/api/trading/strategies` | ❌ | 사용 가능한 전략 목록 조회 |
| `POST` | `/api/trading/strategy` | ✅ | 전략 변경 (다음 tick에 Consumer 자동 교체) |
| `POST` | `/api/trading/symbol` | ✅ | 매매 코인 변경 (매매 중지 상태에서만 허용) |

#### POST `/api/trading/start`
```json
// Response
{"message": "Auto-trading started", "symbol": "BTCUSDT", "mode": "queue"}
```

#### GET `/api/trading/status`
```json
// Response (Consumer 실행 중)
{
  "symbol": "BTCUSDT",
  "strategy": "regime_trend",
  "is_active": true,
  "trading_enabled": true,
  "in_position": false,
  "trailing_stop": null,
  "last_signal": "hold",
  "last_reason": "ema_gap_below_threshold",
  "close_price": 95420.5,
  "bars_since_trade": 12,
  "parameters": {...},
  "updated_at": "2026-03-02T10:00:00Z"
}
```

#### POST `/api/trading/strategy`
```json
// Request
{"strategy": "regime_trend"}
// Available: "regime_trend" | "breakout_volume" | "mean_reversion" | "dual_timeframe"
```

#### POST `/api/trading/symbol`
```json
// Request
{"symbol": "XRPUSDT"}
// Supported: "BTCUSDT" | "XRPUSDT" | "SOLUSDT"
```

---

### Strategy Presets

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/trading/presets` | ✅ | 프리셋 목록 조회 (`?strategy=regime_trend` 선택적 필터) |
| `GET` | `/api/trading/presets/{strategy}/{symbol}` | ✅ | 특정 strategy+symbol 프리셋 조회 |
| `POST` | `/api/trading/presets/{strategy}/{symbol}` | ✅ | 프리셋 저장(upsert) |

#### POST `/api/trading/presets/{strategy}/{symbol}`
```json
// Request
{
  "params": {
    "ema_fast": 50,
    "ema_slow": 200,
    "atr_multiplier": 2.5
  }
}
```

---

### Market Data

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/price/{symbol}` | ❌ | 실시간 가격 조회 |
| `GET` | `/api/chart/{symbol}` | ❌ | 차트 OHLCV 데이터 (`?interval=15&limit=100`) |
| `GET` | `/api/signals` | ❌ | 최근 거래 신호 (`?limit=5`) |

#### GET `/api/chart/{symbol}`
```
Query params:
  interval: str  캔들 간격 (1, 3, 5, 15, 30, 60, 120, 240, D) [기본: "1"]
  limit: int     최대 캔들 수 [기본: 100]
```

---

### Portfolio

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/portfolio` | ❌ | 포트폴리오 현황 (잔고 + 수익률) |
| `GET` | `/api/portfolio/history` | ❌ | 포트폴리오 히스토리 (`?period=7d`) |
| `GET` | `/api/portfolio/performance` | ❌ | 수익률 통계 |
| `GET` | `/api/portfolio/multi-asset` | ❌ | 다중 자산(BTC/XRP/SOL) 현황 |
| `GET` | `/api/portfolio/allocation` | ❌ | 자산 배분 현황 (파이 차트용) |

#### GET `/api/portfolio/history`
```
Query params:
  period: str   조회 기간 ("1d" | "7d" | "30d") [기본: "7d"]
```

---

### Trades & PnL

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/trades` | ❌ | 거래 내역 조회 (`?limit=50`) |
| `GET` | `/api/trades/markers/{symbol}` | ❌ | 차트용 거래 마커 데이터 |
| `GET` | `/api/pnl/{symbol}` | ❌ | 특정 심볼 손익 조회 |

---

### Positions

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/positions` | ❌ | 현재 포지션 조회 (집계) |
| `GET` | `/api/positions/open` | ❌ | 열린 포지션 목록 (`?symbol=BTCUSDT` 선택적) |
| `GET` | `/api/positions/closed` | ❌ | 닫힌 포지션 목록 (`?symbol=BTCUSDT&limit=50`) |
| `GET` | `/api/positions/summary` | ❌ | 포지션 요약 통계 (`?symbol=BTCUSDT` 선택적) |
| `GET` | `/api/positions/{position_id}` | ❌ | 특정 포지션 상세 정보 |
| `POST` | `/api/positions/open` | ✅ | 새 포지션 열기 |
| `POST` | `/api/positions/close` | ✅ | 포지션 닫기 |
| `POST` | `/api/positions/update-prices` | ✅ | 포지션 현재가 업데이트 |
| `POST` | `/api/positions/auto-close` | ✅ | 조건에 따른 자동 포지션 종료 |

#### POST `/api/positions/open`
```json
// Request
{
  "symbol": "BTCUSDT",
  "position_type": "long",
  "entry_price": 95000.0,
  "quantity": 0.001,
  "dollar_amount": 95.0
}
```

#### POST `/api/positions/close`
```json
// Request
{
  "position_id": "pos_abc123",
  "close_price": 96500.0
}
```

#### POST `/api/positions/auto-close`
```
Query params:
  symbol: str              대상 심볼 (선택)
  max_loss_percent: float  최대 손실 % 초과 시 종료 (선택)
  min_profit_percent: float 최소 수익 % 달성 시 종료 (선택)
  max_days_open: int       최대 보유 일수 초과 시 종료 (선택)
```

---

### Manual Order

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/order` | ✅ | 수동 시장가 주문 |

#### POST `/api/order`
```json
// Request (JSON body 또는 Query params)
{
  "symbol": "BTCUSDT",
  "side": "Buy",
  "qty": 0.001
}
// side: "Buy" | "Sell"
```

---

### DB 직접 조회 (관리용)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/db/trades` | ✅ | trades 테이블 조회 (`?limit=100`) |
| `GET` | `/api/db/signal-log` | ✅ | signal_log 테이블 조회 (필터 가능) |
| `GET` | `/api/db/signal-log/stats` | ✅ | 전략별 신호 통계 (HOLD/BUY/SELL 비율) |
| `GET` | `/api/db/positions` | ✅ | positions 테이블 조회 (필터 가능) |
| `GET` | `/api/db/summary` | ✅ | DB 전체 현황 요약 |

#### GET `/api/db/signal-log`
```
Query params:
  strategy: str    전략 이름 필터 (선택)
  symbol: str      심볼 필터 (선택)
  signal: str      신호 필터 ("buy" | "sell" | "hold", 선택)
  limit: int       최대 행 수 [기본: 200]
```

#### GET `/api/db/signal-log/stats`
```json
// Response
{
  "total": 1500,
  "by_signal": {
    "hold": 1380,
    "buy": 78,
    "sell": 42
  },
  "by_strategy": {
    "regime_trend": {"total": 900, "buy": 45, "sell": 28, "hold": 827}
  }
}
```

#### GET `/api/db/positions`
```
Query params:
  status: str    "open" | "closed" (선택)
  symbol: str    심볼 필터 (선택)
  limit: int     최대 행 수 [기본: 100]
```

---

## Error Responses

| Status | Description |
|--------|-------------|
| `401` | API 키 누락 또는 잘못됨 (`X-API-KEY` 헤더 확인) |
| `404` | 리소스 없음 (포지션 ID 등) |
| `422` | 유효하지 않은 파라미터 (심볼, 전략 이름 등) |
| `429` | Rate limit 초과 |
| `500` | 서버 내부 오류 |

---

## Rate Limits

| Endpoint | Limit |
|----------|-------|
| `POST /api/trading/start` | 5/분 |
| `POST /api/trading/stop` | 5/분 |
| `POST /api/trading/strategy` | 5/분 |
| `POST /api/order` | 10/분 |
| `POST /api/trading/presets/{...}` | 10/분 |
| `GET /api/auth/validate` | 10/분 |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `""` (SQLite fallback) | PostgreSQL DSN (`postgresql://user:pw@host/db`) |
| `REDIS_URL` | `""` (in-memory fallback) | Redis URL (`redis://host:6379`) |
| `ADMIN_KEY` | — | API 인증 키 (필수) |
| `STRATEGY_SYMBOL` | `BTCUSDT` | 자동매매 대상 심볼 |
| `QUANT_DB_PATH` | `./data/quant_timeseries.db` | SQLite 사용 시 quant DB 경로 |
| `BYBIT_API_KEY` | — | Bybit API 키 |
| `BYBIT_API_SECRET` | — | Bybit API 시크릿 |
| `DISCORD_WEBHOOK_URL` | — | Discord 알림 웹훅 URL |
