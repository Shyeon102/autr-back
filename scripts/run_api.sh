#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd)/src:$(pwd)/backend:${PYTHONPATH:-}"
exec uvicorn autr.api.app:app --host 0.0.0.0 --port "${PORT:-8000}" --reload
