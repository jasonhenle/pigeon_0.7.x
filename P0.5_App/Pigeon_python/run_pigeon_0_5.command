#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# If the project folder was moved/copied, an old .venv still points pip/scripts at the
# previous path (pyvenv.cfg "command = ... -m venv <path>"). Rebuild in that case.
EXPECTED_VENV="${SCRIPT_DIR}/.venv"
if [[ -f "${EXPECTED_VENV}/pyvenv.cfg" ]]; then
  VENV_FROM_CFG="$(grep '^command = ' "${EXPECTED_VENV}/pyvenv.cfg" 2>/dev/null | sed -n 's/.* -m venv //p' | tr -d '\r')"
  if [[ -n "${VENV_FROM_CFG}" && "${VENV_FROM_CFG}" != "${EXPECTED_VENV}" ]]; then
    echo "Rebuilding Python environment (project was moved from another folder)..." >&2
    rm -rf "${EXPECTED_VENV}"
  fi
fi

VENV_BIN="${SCRIPT_DIR}/.venv/bin"
if [[ ! -d "${SCRIPT_DIR}/.venv" ]]; then
  python3 -m venv "${SCRIPT_DIR}/.venv"
fi

# Use the venv interpreter directly — `python` is often missing on macOS outside the venv.
PY="${VENV_BIN}/python3"
[[ -x "$PY" ]] || PY="${VENV_BIN}/python"
[[ -x "$PY" ]] || {
  echo "No usable Python in .venv. Install Python 3, then: python3 -m venv .venv" >&2
  exit 1
}

"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r requirements.txt

exec "$PY" pigeon_0_5.py "$@"
