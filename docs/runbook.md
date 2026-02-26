# Runbook

## API not healthy

1. Check process logs from `scripts/run_api.sh`.
2. Verify `/health` and `/ready` endpoints.
3. Validate `.env` and Bybit key settings.

## Strategy loop stuck

1. Hit `/api/trading/status`.
2. Restart strategy with `/api/trading/stop` then `/api/trading/start`.
3. Inspect `data/trades.db` and last `signal_log` rows.

## Data pipeline failure

1. Run `./scripts/run_collector.sh` once.
2. Check `data/quant_timeseries.db` and `pipeline_runs` status.
