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
