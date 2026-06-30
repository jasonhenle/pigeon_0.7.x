#!/bin/bash
# Pigeon 0.7 launcher for Linux / Raspberry Pi OS.
set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${INSTALLER_DIR}/.." && pwd)"
cd "${ROOT}"
SYSTEM_DIR="${ROOT}/pigeonSystem"
VENV_REBUILD_REASON=""

python_is_supported() {
  local py="${1:-}"
  [[ -n "${py}" && -x "${py}" ]] || return 1
  "${py}" -c "import sys, tkinter; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1
}

pick_python_with_tk() {
  local candidate=""
  local resolved=""
  for candidate in \
    python3 python3.13 python3.12 python3.11 python3.10 \
    /usr/bin/python3 /usr/local/bin/python3; do
    if [[ "${candidate}" == /* ]]; then
      [[ -x "${candidate}" ]] || continue
      resolved="${candidate}"
    else
      command -v "${candidate}" >/dev/null 2>&1 || continue
      resolved="$(command -v "${candidate}")"
    fi
    if python_is_supported "${resolved}"; then
      "${resolved}" -c "import sys; print(sys.executable)"
      return 0
    fi
  done
  return 1
}

MAIN_PY="${SYSTEM_DIR}/pigeon_0_7.py"
WAVES_PY="${SYSTEM_DIR}/pigeon/audio_waves.py"
LEGACY_VIZ="${SYSTEM_DIR}/pigeon/mic_wave_visualizer.py"
if [[ -f "${MAIN_PY}" ]] && grep -q 'pigeon\.mic_wave_visualizer' "${MAIN_PY}" 2>/dev/null; then
  sed -i 's/from pigeon\.mic_wave_visualizer import blend_mic_visualizer/from pigeon.audio_waves import blend_mic_visualizer/g' "${MAIN_PY}" || true
fi
[[ ! -f "${LEGACY_VIZ}" ]] || rm -f "${LEGACY_VIZ}"
if [[ ! -f "${WAVES_PY}" ]]; then
  echo "pigeon: missing pigeon/audio_waves.py — copy or pull the latest Pigeon build." >&2
  exit 1
fi

EXPECTED_VENV="${SYSTEM_DIR}/.venv"
if [[ -f "${EXPECTED_VENV}/pyvenv.cfg" ]]; then
  VENV_FROM_CFG="$(grep '^command = ' "${EXPECTED_VENV}/pyvenv.cfg" 2>/dev/null | sed -n 's/.* -m venv //p' | tr -d '\r' || true)"
  if [[ -n "${VENV_FROM_CFG}" && "${VENV_FROM_CFG}" != "${EXPECTED_VENV}" ]]; then
    echo "pigeon: detected venv path mismatch for this machine. Rebuilding..." >&2
    VENV_REBUILD_REASON="path-mismatch"
    rm -rf "${EXPECTED_VENV}"
  fi
fi

VENV_BIN="${SYSTEM_DIR}/.venv/bin"

if [[ ! -d "${SYSTEM_DIR}/.venv" ]]; then
  BASE_PY="$(pick_python_with_tk || true)"
  if [[ -z "${BASE_PY}" ]]; then
    echo "pigeon: no Python 3.10+ with tkinter found." >&2
    echo "pigeon: on Raspberry Pi OS run: sudo apt install python3 python3-venv python3-tk" >&2
    echo "pigeon: or run installer/install_on_pi.sh from this folder." >&2
    exit 1
  fi
  [[ -n "${VENV_REBUILD_REASON}" ]] || VENV_REBUILD_REASON="missing-venv"
  "${BASE_PY}" -m venv "${SYSTEM_DIR}/.venv"
fi

PY="${VENV_BIN}/python3"
[[ -x "$PY" ]] || PY="${VENV_BIN}/python"
if [[ ! -x "$PY" ]]; then
  BASE_PY="$(pick_python_with_tk || true)"
  [[ -n "${BASE_PY}" ]] || {
    echo "pigeon: no Python 3.10+ with tkinter found." >&2
    echo "pigeon: on Raspberry Pi OS run: sudo apt install python3 python3-venv python3-tk" >&2
    exit 1
  }
  echo "pigeon: venv interpreter is broken on this machine. Rebuilding..." >&2
  VENV_REBUILD_REASON="broken-interpreter"
  rm -rf "${SYSTEM_DIR}/.venv"
  "${BASE_PY}" -m venv "${SYSTEM_DIR}/.venv"
  PY="${VENV_BIN}/python3"
  [[ -x "$PY" ]] || PY="${VENV_BIN}/python"
fi

if ! python_is_supported "$PY"; then
  BASE_PY="$(pick_python_with_tk || true)"
  [[ -n "${BASE_PY}" ]] || {
    echo "pigeon: current venv python is not usable (needs Python 3.10+ with tkinter)." >&2
    echo "pigeon: on Raspberry Pi OS run: sudo apt install python3 python3-venv python3-tk" >&2
    exit 1
  }
  echo "pigeon: venv python is missing tkinter or too old. Rebuilding..." >&2
  VENV_REBUILD_REASON="unsupported-venv-python"
  rm -rf "${SYSTEM_DIR}/.venv"
  "${BASE_PY}" -m venv "${SYSTEM_DIR}/.venv"
  PY="${VENV_BIN}/python3"
  [[ -x "$PY" ]] || PY="${VENV_BIN}/python"
fi
[[ -x "$PY" ]] || {
  echo "No usable Python in .venv. Install Python 3, then: python3 -m venv .venv" >&2
  exit 1
}

"$PY" -m pip install --upgrade pip >/dev/null
REQ_FILE="${SYSTEM_DIR}/requirements.txt"
if [[ "$(uname -s)" == "Linux" && -f "${SYSTEM_DIR}/requirements-pi.txt" ]]; then
  REQ_FILE="${SYSTEM_DIR}/requirements-pi.txt"
fi
"$PY" -m pip install -r "${REQ_FILE}"

if [[ "${1:-}" == "--bootstrap-only" ]]; then
  echo "pigeon: python environment ready." >&2
  exit 0
fi

if [[ -n "${VENV_REBUILD_REASON}" ]]; then
  echo "pigeon: python environment ready (${VENV_REBUILD_REASON})." >&2
fi
echo "Starting Pigeon…" >&2
export PYTHONPYCACHEPREFIX="${ROOT}/pigeonCashe"
# Pi / Linux: 800×480 logical target; UI letterboxes 800×400 with black bars. Fullscreen fills the monitor.
export PIGEON_DISPLAY_W="${PIGEON_DISPLAY_W:-800}"
export PIGEON_DISPLAY_H="${PIGEON_DISPLAY_H:-480}"
export PIGEON_WINDOW_SCALE="${PIGEON_WINDOW_SCALE:-1.0}"
export PIGEON_PI_FULLSCREEN="${PIGEON_PI_FULLSCREEN:-1}"
export PIGEON_APPLE_TV_SCAN_TIMEOUT="${PIGEON_APPLE_TV_SCAN_TIMEOUT:-12}"

STATE_DIR="${HOME}/.pigeon_0_6"
mkdir -p "${STATE_DIR}"
if [[ ! -f "${STATE_DIR}/tmdb_api_key" && ! -f "${STATE_DIR}/tmdb_read_token" ]]; then
  if [[ -f "${ROOT}/installer/setup/tmdb_api_key" ]]; then
    cp "${ROOT}/installer/setup/tmdb_api_key" "${STATE_DIR}/tmdb_api_key"
    chmod 600 "${STATE_DIR}/tmdb_api_key" 2>/dev/null || true
  elif [[ -f "${ROOT}/installer/setup/tmdb_read_token" ]]; then
    cp "${ROOT}/installer/setup/tmdb_read_token" "${STATE_DIR}/tmdb_read_token"
    chmod 600 "${STATE_DIR}/tmdb_read_token" 2>/dev/null || true
  fi
fi

exec "$PY" -u "${MAIN_PY}" "$@"
