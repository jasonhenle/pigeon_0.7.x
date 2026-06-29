#!/bin/bash
# Build a Raspberry Pi–ready tarball from this Mac (or any Linux host).
# Output: raspberryPi/dist/pigeon_0.7.0_raspberry_pi.tar.gz
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIGEON_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${SCRIPT_DIR}/dist"
STAGING="${DIST_DIR}/staging"
ARCHIVE_NAME="pigeon_0.7.0_raspberry_pi.tar.gz"

rm -rf "${STAGING}"
mkdir -p "${STAGING}" "${DIST_DIR}"

echo "==> Staging Pigeon build (excluding Mac venv, caches, and local TMDB art)…"
rsync -a \
  --exclude '.DS_Store' \
  --exclude 'pigeonSystem/.venv' \
  --exclude 'pigeonSystem/__pycache__' \
  --exclude 'pigeonSystem/.cursor' \
  --exclude 'pigeonSystem/pigeon/**/__pycache__' \
  --exclude 'pigeonCashe' \
  --exclude 'raspberryPi/dist' \
  --exclude 'pigeonTMDB/pigeonTMDB_BD' \
  --exclude 'pigeonTMDB/pigeonTMDB_ORIGINAL' \
  --exclude 'pigeonTMDB/pigeonTMDB_Poster' \
  --exclude 'pigeonTMDB/pigeonTMDB_TT' \
  --exclude 'pigeonTMDB/*.jpg' \
  --exclude 'pigeonTMDB/*.png' \
  --exclude 'install_pigeon.command' \
  --exclude 'install-pigeon.sh' \
  --exclude 'Install-Pigeon.desktop' \
  --exclude 'Run-Pigeon.desktop' \
  --exclude 'installer/install_mac.sh' \
  "${PIGEON_ROOT}/" "${STAGING}/Pigeon_0.7.0/"

# Guard against empty or partial staging (e.g. wrong cwd, missing assets after a bad copy).
STAGING_APP="${STAGING}/Pigeon_0.7.0"
STAGING_BYTES="$(du -sk "${STAGING_APP}" | awk '{print $1}')"
if [[ ! -f "${STAGING_APP}/pigeonSystem/pigeon_0_7.py" ]]; then
  echo "ERROR: staging is missing pigeonSystem/pigeon_0_7.py — run from a full Pigeon_0.7.0 tree." >&2
  exit 1
fi
if [[ ! -d "${STAGING_APP}/pigeonAssets/App logos" ]]; then
  echo "ERROR: staging is missing pigeonAssets — app tree looks incomplete." >&2
  exit 1
fi
if [[ "${STAGING_BYTES}" -lt 20480 ]]; then
  echo "ERROR: staging is only ${STAGING_BYTES} KB (expected tens of MB). Aborting." >&2
  exit 1
fi

mkdir -p \
  "${STAGING}/Pigeon_0.7.0/pigeonCashe" \
  "${STAGING}/Pigeon_0.7.0/pigeonTMDB/pigeonTMDB_BD" \
  "${STAGING}/Pigeon_0.7.0/pigeonTMDB/pigeonTMDB_ORIGINAL" \
  "${STAGING}/Pigeon_0.7.0/pigeonTMDB/pigeonTMDB_Poster" \
  "${STAGING}/Pigeon_0.7.0/pigeonTMDB/pigeonTMDB_TT"

chmod +x \
  "${STAGING}/Pigeon_0.7.0/run_pigeon_0_7.sh" \
  "${STAGING}/Pigeon_0.7.0/install_pigeon.sh" \
  "${STAGING}/Pigeon_0.7.0/run-pigeon.sh" \
  "${STAGING}/Pigeon_0.7.0/Install-Pigeon" \
  "${STAGING}/Pigeon_0.7.0/Run-Pigeon" \
  "${STAGING}/Pigeon_0.7.0/installer/click_install_pi.sh" \
  "${STAGING}/Pigeon_0.7.0/installer/click_run_pigeon_pi.sh" \
  "${STAGING}/Pigeon_0.7.0/raspberryPi/install_on_pi.sh"

echo "==> Writing ${DIST_DIR}/${ARCHIVE_NAME}"
tar -C "${STAGING}" -czf "${DIST_DIR}/${ARCHIVE_NAME}" Pigeon_0.7.0

BYTES="$(wc -c < "${DIST_DIR}/${ARCHIVE_NAME}" | tr -d ' ')"
if [[ "${BYTES}" -lt 1048576 ]]; then
  echo "ERROR: ${ARCHIVE_NAME} is only ${BYTES} bytes — tarball looks empty or corrupt." >&2
  exit 1
fi
MB="$(awk "BEGIN {printf \"%.1f\", ${BYTES}/1048576}")"
echo ""
echo "Done: ${DIST_DIR}/${ARCHIVE_NAME} (${MB} MB)"
echo ""
echo "Copy to the Pi, then on the Pi:"
echo "  tar -xzf ${ARCHIVE_NAME}"
echo "  Open the Pigeon_0.7.0 folder and double-click Install-Pigeon"
