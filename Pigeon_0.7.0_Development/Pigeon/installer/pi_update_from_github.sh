#!/bin/bash
# Update Pigeon on Raspberry Pi from GitHub without the in-app updater.
# Use when Updates fails with a latin-1 / U+202F encoding error (bad pasted token).
#
# Usage:
#   bash installer/pi_update_from_github.sh
#   bash installer/pi_update_from_github.sh /home/pi/Pigeon_0.7.16
#
# Requires: curl, unzip or python3, rsync (install via apt if missing).
set -euo pipefail

REPO="${PIGEON_UPDATE_GITHUB_USER:-jasonhenle}/${PIGEON_UPDATE_GITHUB_REPO:-pigeon_0.7.x}"
BRANCH="${PIGEON_UPDATE_GITHUB_BRANCH:-main}"
ZIP_URL="https://codeload.github.com/${REPO}/zip/refs/heads/${BRANCH}"

INSTALL_DIR="${1:-}"
if [[ -z "${INSTALL_DIR}" ]]; then
  for d in "${HOME}"/Pigeon_*; do
    if [[ -f "${d}/installer/run_pigeon_0_7.sh" && -f "${d}/pigeonSystem/pigeon_0_7.py" ]]; then
      INSTALL_DIR="${d}"
      break
    fi
  done
fi

if [[ -z "${INSTALL_DIR}" || ! -d "${INSTALL_DIR}" ]]; then
  echo "pigeon: could not find Pigeon install folder." >&2
  echo "Usage: $0 /path/to/Pigeon_X.Y.Z" >&2
  exit 1
fi

INSTALL_DIR="$(cd "${INSTALL_DIR}" && pwd)"
echo "==> Updating Pigeon at ${INSTALL_DIR}"
echo "==> Downloading ${ZIP_URL}"

# Remove bad GitHub tokens (narrow no-break space U+202F breaks old updaters).
rm -f "${HOME}/.pigeon_0_6/github_update_token"
unset PIGEON_UPDATE_GITHUB_TOKEN GITHUB_TOKEN GH_TOKEN GITHUB_PAT 2>/dev/null || true

for cmd in curl rsync python3; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "pigeon: ${cmd} is required. Run: sudo apt install curl rsync python3" >&2
    exit 1
  fi
done

WORKDIR="$(mktemp -d /tmp/pigeon-update.XXXXXX)"
trap 'rm -rf "${WORKDIR}"' EXIT

curl -fsSL -o "${WORKDIR}/pigeon.zip" "${ZIP_URL}"
python3 - <<'PY' "${WORKDIR}/pigeon.zip" "${WORKDIR}/extract"
import sys
import zipfile
from pathlib import Path

zip_path, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(zip_path) as zf:
    zf.extractall(out)
PY

SRC=""
for d in "${WORKDIR}/extract"/*; do
  if [[ -d "${d}/pigeonSystem" ]]; then
    SRC="${d}"
    break
  fi
  if [[ -d "${d}" ]]; then
    for sub in "${d}"/*; do
      if [[ -f "${sub}/pigeonSystem/pigeon_0_7.py" || -f "${sub}/pigeonSystem/pigeon_0_6.py" ]]; then
        SRC="${sub}"
        break 2
      fi
    done
  fi
done

if [[ -z "${SRC}" || ! -d "${SRC}/pigeonSystem" ]]; then
  echo "pigeon: could not find app folder inside GitHub zip." >&2
  exit 1
fi

echo "==> Merging app code and UI assets into ${INSTALL_DIR} (settings in ~/.pigeon_0_6 are not touched)…"
rsync -a \
  --exclude 'pigeonSystem/.venv' \
  --exclude 'pigeonCashe' \
  --exclude 'pigeonTMDB' \
  --exclude 'raspberryPi/dist' \
  --exclude 'pigeonSystem/__pycache__' \
  --exclude '.DS_Store' \
  "${SRC}/" "${INSTALL_DIR}/"

if [[ -d "${SRC}/pigeonAssets" ]]; then
  echo "==> Refreshing pigeonAssets (status bar, logos, poster chrome)…"
  rsync -a "${SRC}/pigeonAssets/" "${INSTALL_DIR}/pigeonAssets/"
fi

# shellcheck source=common.sh
source "${INSTALL_DIR}/installer/common.sh"
pigeon_install_bundled_fonts "${INSTALL_DIR}" "${HOME}"

echo "==> Refreshing Python dependencies…"
bash "${INSTALL_DIR}/installer/run_pigeon_0_7.sh" --bootstrap-only

VER="$(python3 -c "import importlib.util; p='${INSTALL_DIR}/pigeonSystem/pigeon/version.py'; s=importlib.util.spec_from_file_location('pv', p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.version_string())")"
echo ""
echo "Pigeon ${VER} installed. Quit and relaunch Pigeon to run the new version."
