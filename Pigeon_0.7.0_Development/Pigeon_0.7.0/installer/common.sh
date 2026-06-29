#!/bin/bash
# Shared helpers for Pigeon installers (macOS + Linux).
set -euo pipefail

pigeon_version_string() {
  local version_py="${1}/pigeonSystem/pigeon/version.py"
  local version=""
  if [[ -f "${version_py}" ]]; then
    version="$(python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location('pv','${version_py}'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(m.version_string())" 2>/dev/null || true)"
  fi
  if [[ -n "${version}" ]]; then
    echo "${version}"
  else
    echo "0.7.0"
  fi
}

pigeon_rsync_tree() {
  local src="${1}"
  local dest="${2}"
  mkdir -p "${dest}"
  rsync -a --delete \
    --exclude '.DS_Store' \
    --exclude 'pigeonSystem/.venv' \
    --exclude 'pigeonSystem/__pycache__' \
    --exclude 'pigeonSystem/.cursor' \
    --exclude 'pigeonSystem/pigeon/**/__pycache__' \
    --exclude 'pigeonCashe' \
    --exclude 'raspberryPi/dist' \
    "${src}/" "${dest}/"
}

pigeon_prepare_runtime_dirs() {
  local root="${1}"
  mkdir -p \
    "${root}/pigeonCashe" \
    "${root}/pigeonTMDB/pigeonTMDB_BD" \
    "${root}/pigeonTMDB/pigeonTMDB_ORIGINAL" \
    "${root}/pigeonTMDB/pigeonTMDB_Poster" \
    "${root}/pigeonTMDB/pigeonTMDB_TT"
}

pigeon_print_usage() {
  cat <<'EOF'
Pigeon installer

Usage:
  ./install_pigeon.sh [options]

Options:
  --dir PATH       Install location (default: platform-specific)
  --in-place       Install using the current folder (no copy)
  --no-shortcut    Skip Desktop shortcut (macOS only)
  --no-autostart   Skip systemd autostart setup (Linux / Pi only)
  -h, --help       Show this help

macOS default install dir:  ~/Applications/Pigeon_0.7.0
Linux / Pi default dir:   ~/Pigeon_0.7.0

After install, launch with:
  macOS:  ~/Desktop/Pigeon.command   (or run_pigeon_0_7.command in the install folder)
  Linux:  ./run_pigeon_0_7.sh        (in the install folder)
EOF
}
