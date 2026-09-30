#!/usr/bin/env bash
# Run all automated tests. Uses no network and never places orders.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "  Start eerst één keer ./start.sh zodat de Python-omgeving bestaat."
  exit 1
fi
exec .venv/bin/python -m pytest -q "$@"
