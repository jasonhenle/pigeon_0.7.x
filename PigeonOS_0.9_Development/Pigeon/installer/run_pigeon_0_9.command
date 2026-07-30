#!/bin/bash
# PigeonOS 0.9 launcher (wraps the current pigeon_0_8.py entrypoint).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "${DIR}/run_pigeon_0_8.command"
