#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEM_DIR="${DEV_DIR}/Pigeon/pigeonSystem"
cd "$DEV_DIR"

VENV_BIN="${SYSTEM_DIR}/.venv/bin"
if [[ ! -d "${SYSTEM_DIR}/.venv" ]]; then
  python3 -m venv "${SYSTEM_DIR}/.venv"
fi

PY="${VENV_BIN}/python3"
[[ -x "$PY" ]] || PY="${VENV_BIN}/python"
[[ -x "$PY" ]] || {
  echo "No usable Python in pigeonSystem/.venv. Install Python 3, then re-run." >&2
  exit 1
}

"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r "${SYSTEM_DIR}/requirements.txt" >/dev/null
"$PY" -m pip install pymupdf >/dev/null

exec "$PY" testingEnvironments/settings_page_test.py "$@"
