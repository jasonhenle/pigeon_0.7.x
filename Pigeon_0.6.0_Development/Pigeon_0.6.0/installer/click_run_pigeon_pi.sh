#!/bin/bash
# Double-click launcher for Pigeon on Raspberry Pi OS.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
INSTALL_DIR="${PIGEON_INSTALL_DIR:-${HOME}/Pigeon_0.6.0}"

if [[ -x "${INSTALL_DIR}/run_pigeon_0_6.sh" ]]; then
  cd "${INSTALL_DIR}"
  exec ./run_pigeon_0_6.sh
fi

if [[ -x "${ROOT}/run_pigeon_0_6.sh" ]]; then
  cd "${ROOT}"
  exec ./run_pigeon_0_6.sh
fi

if command -v zenity >/dev/null 2>&1; then
  zenity --no-markup --error --title="Pigeon" \
    --text="Pigeon is not installed yet.

Double-click Install-Pigeon first."
else
  printf 'Pigeon is not installed. Run Install Pigeon first.\n' >&2
fi
exit 1
