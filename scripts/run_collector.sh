#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$(pwd)/src:$(pwd)/backend:${PYTHONPATH:-}"
exec python -m autr.services.run_collector "$@"
