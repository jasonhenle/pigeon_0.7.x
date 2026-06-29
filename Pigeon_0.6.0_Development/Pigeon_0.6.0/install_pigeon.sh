#!/bin/bash
# Pigeon installer — detects macOS vs Linux / Raspberry Pi and runs the right steps.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

show_help() {
  # shellcheck source=installer/common.sh
  source "${ROOT}/installer/common.sh"
  pigeon_print_usage
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

case "$(uname -s)" in
  Darwin)
    exec bash "${ROOT}/installer/install_mac.sh" "$@"
    ;;
  Linux)
    if [[ -f "${ROOT}/raspberryPi/install_on_pi.sh" ]]; then
      exec bash "${ROOT}/raspberryPi/install_on_pi.sh" "$@"
    fi
    echo "pigeon: Linux installer not found in this build." >&2
    exit 1
    ;;
  *)
    echo "pigeon: unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac
