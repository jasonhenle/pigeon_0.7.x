#!/bin/bash
# Canonical GitHub updater for Pigeon (Pi / Linux). Always fetch fresh from GitHub:
#   curl -fsSL "$URL" | bash -s -- /path/to/Pigeon_X.Y.Z
#
# Uses curl + rsync only — no Python http.client (avoids latin-1 / U+202F token errors).
set -euo pipefail

REPO="${PIGEON_UPDATE_GITHUB_USER:-jasonhenle}/${PIGEON_UPDATE_GITHUB_REPO:-pigeon_0.7.x}"
BRANCH="${PIGEON_UPDATE_GITHUB_BRANCH:-main}"
ZIP_URL="https://codeload.github.com/${REPO}/zip/refs/heads/${BRANCH}"
APP_REL="Pigeon_0.8.0_Development/Pigeon"
STATE_DIR="${HOME}/.pigeon_0_6"
LOG_FILE="${STATE_DIR}/pigeon.log"

INSTALL_DIR="${1:-${PIGEON_INSTALL_ROOT:-}}"
if [[ -z "${INSTALL_DIR}" ]]; then
  for d in "${HOME}"/Pigeon_*; do
    if [[ -f "${d}/installer/run_pigeon_0_8.sh" && -f "${d}/pigeonSystem/pigeon_0_8.py" ]]; then
      INSTALL_DIR="${d}"
      break
    fi
  done
fi

log() {
  local line="pigeon-update: $*"
  echo "${line}"
  mkdir -p "${STATE_DIR}"
  printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${line}" >> "${LOG_FILE}" 2>/dev/null || true
}

die() {
  log "ERROR: $*"
  echo "pigeon-update ERROR: $*" >&2
  exit 1
}

if [[ -z "${INSTALL_DIR}" || ! -d "${INSTALL_DIR}" ]]; then
  die "could not find Pigeon install folder. Usage: bash pigeon_github_update.sh /path/to/Pigeon_X.Y.Z"
fi

INSTALL_DIR="$(cd "${INSTALL_DIR}" && pwd)"
log "starting update for ${INSTALL_DIR}"
log "zip ${ZIP_URL}"

rm -f "${STATE_DIR}/github_update_token" 2>/dev/null || true
unset PIGEON_UPDATE_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN GITHUB_PAT 2>/dev/null || true

for cmd in curl rsync python3 bash; do
  command -v "${cmd}" >/dev/null 2>&1 || die "${cmd} missing — run: sudo apt install curl rsync python3"
done

WORKDIR="$(mktemp -d /tmp/pigeon-github-update.XXXXXX)"
trap 'rm -rf "${WORKDIR}"' EXIT

curl -fsSL -o "${WORKDIR}/pigeon.zip" "${ZIP_URL}" || die "curl download failed (network or GitHub blocked)"

python3 - <<'PY' "${WORKDIR}/pigeon.zip" "${WORKDIR}/extract" "${APP_REL}"
import sys
import zipfile
from pathlib import Path

zip_path, out, app_rel = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
marker = app_rel.rstrip("/") + "/"
out.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(zip_path) as zf:
    for info in zf.infolist():
        name = info.filename
        if marker not in name:
            continue
        target = out / name
        if info.is_dir() or name.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            dst.write(src.read())
PY

EXTRACT="${WORKDIR}/extract"
SRC=""
for candidate in \
  "${EXTRACT}"/*/"${APP_REL}" \
  "${EXTRACT}/${APP_REL}" \
  "${EXTRACT}"/*; do
  if [[ -f "${candidate}/pigeonSystem/pigeon_0_8.py" ]]; then
    SRC="${candidate}"
    break
  fi
done

if [[ -z "${SRC}" || ! -d "${SRC}/pigeonSystem" ]]; then
  die "could not find ${APP_REL} inside GitHub zip"
fi

log "merging from ${SRC}"
rsync -a \
  --exclude 'pigeonSystem/.venv' \
  --exclude 'pigeonCashe' \
  --exclude 'pigeonTMDB' \
  --exclude 'raspberryPi/dist' \
  --exclude 'pigeonSystem/__pycache__' \
  --exclude '.DS_Store' \
  "${SRC}/" "${INSTALL_DIR}/"

if [[ -d "${SRC}/pigeonAssets" ]]; then
  log "refreshing pigeonAssets"
  mkdir -p "${INSTALL_DIR}/pigeonAssets"
  rsync -a "${SRC}/pigeonAssets/" "${INSTALL_DIR}/pigeonAssets/"
fi

# GitHub zip extraction drops Unix +x bits; desktop double-click launchers need them.
if [[ -d "${INSTALL_DIR}/installer" ]]; then
  chmod +x "${INSTALL_DIR}/installer/"*.sh 2>/dev/null || true
  chmod +x "${INSTALL_DIR}/installer/Run-Pigeon" "${INSTALL_DIR}/installer/Install-Pigeon" 2>/dev/null || true
fi

log "running pip bootstrap"
bash "${INSTALL_DIR}/installer/run_pigeon_0_8.sh" --bootstrap-only || die "pip bootstrap failed"

VER="$(python3 -c "import importlib.util; p='${INSTALL_DIR}/pigeonSystem/pigeon/version.py'; s=importlib.util.spec_from_file_location('pv', p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.version_string())")"
log "finished — Pigeon ${VER}"
echo ""
echo "Pigeon ${VER} installed."

if [[ "${PIGEON_UPDATE_IN_APP:-}" == "1" ]]; then
  log "in-app update — Pigeon will restart from the running app"
  echo "Restarting Pigeon…"
  exit 0
fi

log "restarting pigeon"
if command -v systemctl >/dev/null 2>&1; then
  if sudo -n systemctl restart pigeon.service 2>/dev/null \
    || systemctl restart pigeon.service 2>/dev/null \
    || sudo -n systemctl restart pigeon 2>/dev/null \
    || systemctl restart pigeon 2>/dev/null; then
    log "systemd restart ok"
    echo "Pigeon restarted via systemd."
    exit 0
  fi
fi

LAUNCHER="${INSTALL_DIR}/installer/run_pigeon_0_8.sh"
if [[ ! -x "${LAUNCHER}" ]]; then
  LAUNCHER="${INSTALL_DIR}/installer/click_run_pigeon_pi.sh"
fi
if [[ -x "${LAUNCHER}" ]]; then
  log "detached relaunch via ${LAUNCHER}"
  nohup bash "${LAUNCHER}" >/dev/null 2>&1 &
  echo "Pigeon relaunched."
else
  echo "Restart manually: bash ${INSTALL_DIR}/installer/run_pigeon_0_8.sh"
fi
