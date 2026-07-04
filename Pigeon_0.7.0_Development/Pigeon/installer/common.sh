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
    echo "0.7.5"
  fi
}

pigeon_install_dir_basename() {
  local root="${1}"
  echo "Pigeon_$(pigeon_version_string "${root}")"
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

pigeon_install_bundled_fonts() {
  local root="${1}"
  local user_home="${2}"
  local src="${root}/pigeonAssets/fonts"
  local dst="${user_home}/.local/share/fonts/pigeon"
  if [[ ! -d "${src}" ]]; then
    return 0
  fi
  mkdir -p "${dst}"
  shopt -s nullglob
  local f
  for f in "${src}"/*.otf "${src}"/*.ttf "${src}"/*.ttc; do
    [[ -f "${f}" ]] || continue
    cp -f "${f}" "${dst}/"
  done
  shopt -u nullglob
  if [[ -f "${src}/fonts.conf" ]]; then
    cp -f "${src}/fonts.conf" "${dst}/fonts.conf"
  fi
  if command -v fc-cache >/dev/null 2>&1; then
    fc-cache -f "${dst}" >/dev/null 2>&1 || true
  fi
}

# Passwordless ``systemctl restart pigeon`` for in-app GitHub updates (Pi autostart).
pigeon_install_systemd_restart_sudoers() {
  local install_user="${1:-}"
  local systemctl_bin=""
  local dropin="/etc/sudoers.d/pigeon-update-restart"
  if [[ -z "${install_user}" ]]; then
    return 0
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    return 0
  fi
  systemctl_bin="$(command -v systemctl)"
  cat > "${dropin}" <<EOF
# Pigeon: allow in-app GitHub updates to restart the systemd service without a password.
${install_user} ALL=(root) NOPASSWD: ${systemctl_bin} restart pigeon.service, ${systemctl_bin} restart pigeon
EOF
  chmod 0440 "${dropin}"
  if visudo -cf "${dropin}" >/dev/null 2>&1; then
    echo "==> Passwordless sudo for pigeon restart: ${dropin}"
    return 0
  fi
  rm -f "${dropin}"
  echo "pigeon: WARNING — could not install sudoers drop-in; in-app restart may ask for a password." >&2
  return 1
}

pigeon_print_usage() {
  local root="${1:-}"
  local ver="0.7.5"
  if [[ -n "${root}" ]]; then
    ver="$(pigeon_version_string "${root}")"
  fi
  cat <<EOF
Pigeon installer

Usage:
  ./installer/install_pigeon.sh [options]

Options:
  --dir PATH       Install location (default: platform-specific)
  --in-place       Install using the current folder (no copy)
  --no-shortcut    Skip Desktop shortcut (macOS only)
  --no-autostart   Skip systemd autostart setup (Linux / Pi only)
  -h, --help       Show this help

macOS default install dir:  ~/Applications/Pigeon_${ver}
Linux / Pi default dir:   ~/Pigeon_${ver}

After install, launch with:
  macOS:  ~/Desktop/Pigeon.command   (or installer/run_pigeon_0_7.command in the install folder)
  Linux:  ./installer/run_pigeon_0_7.sh (in the install folder)
EOF
}
