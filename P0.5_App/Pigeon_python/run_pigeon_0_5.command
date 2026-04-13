#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Old trees may still import the removed ``mic_wave_visualizer`` module (SyntaxError on Py 3.14+).
# Patch the import and drop the legacy file so launch works even if pigeon_0_5.py was not re-copied.
MAIN_PY="${SCRIPT_DIR}/pigeon_0_5.py"
WAVES_PY="${SCRIPT_DIR}/pigeon/audio_waves.py"
LEGACY_VIZ="${SCRIPT_DIR}/pigeon/mic_wave_visualizer.py"
if [[ -f "${MAIN_PY}" ]] && grep -q 'pigeon\.mic_wave_visualizer' "${MAIN_PY}" 2>/dev/null; then
  sed -i '' 's/from pigeon\.mic_wave_visualizer import blend_mic_visualizer/from pigeon.audio_waves import blend_mic_visualizer/g' "${MAIN_PY}" || true
fi
[[ ! -f "${LEGACY_VIZ}" ]] || rm -f "${LEGACY_VIZ}"
if [[ ! -f "${WAVES_PY}" ]]; then
  echo "pigeon: missing pigeon/audio_waves.py — copy or pull the latest Pigeon_python folder." >&2
  exit 1
fi

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

echo "Starting Pigeon…" >&2
# -u: unbuffered stderr so startup lines (e.g. pigeon: running script) appear immediately.
exec "$PY" -u pigeon_0_5.py "$@"
