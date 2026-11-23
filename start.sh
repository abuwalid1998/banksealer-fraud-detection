#!/usr/bin/env bash
# One-command local start: creates a virtualenv, installs dependencies, runs VeriShield on http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "Creating virtual environment (.venv)..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-verishield.txt
exec python run_verishield.py "$@"
