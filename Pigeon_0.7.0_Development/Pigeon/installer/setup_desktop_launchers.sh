#!/usr/bin/env bash
# Create side-by-side git worktrees + two Desktop double-click launchers:
#   Pigeon (experiment).command  -> ~/Desktop/Pigeon (experiment branch)
#   Pigeon (main).command          -> ~/Desktop/Pigeon-main (main branch)
#
# Run once (or again after moving the repo):
#   bash installer/setup_desktop_launchers.sh

set -euo pipefail

DESKTOP="${HOME}/Desktop"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "setup_desktop_launchers: run from inside the Pigeon git repo" >&2
  exit 1
}

EXPERIMENT_ROOT="${REPO_ROOT}"
MAIN_ROOT="${DESKTOP}/Pigeon-main"
EXPERIMENT_APP="${EXPERIMENT_ROOT}/Pigeon_0.7.0_Development/Pigeon"
MAIN_APP="${MAIN_ROOT}/Pigeon_0.7.0_Development/Pigeon"
LAUNCHER_EXPERIMENT="${DESKTOP}/Pigeon (experiment).command"
LAUNCHER_MAIN="${DESKTOP}/Pigeon (main).command"

echo "==> Pigeon desktop launchers"
echo "    repo:       ${REPO_ROOT}"
echo "    experiment: ${EXPERIMENT_APP}"
echo "    main:       ${MAIN_APP}"

cd "${REPO_ROOT}"
git fetch origin experiment main

# Keep the primary folder on experiment (dev default).
current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [[ "${current_branch}" != "experiment" ]]; then
  echo "==> Checking out experiment in ${REPO_ROOT}"
  git checkout experiment
fi
git pull --ff-only origin experiment || echo "    (experiment pull skipped — local changes or offline)"

if [[ -d "${MAIN_ROOT}/.git" ]] || [[ -f "${MAIN_ROOT}/.git" ]]; then
  echo "==> Updating main worktree at ${MAIN_ROOT}"
  git -C "${MAIN_ROOT}" fetch origin main
  git -C "${MAIN_ROOT}" checkout main
  git -C "${MAIN_ROOT}" pull --ff-only origin main || true
else
  echo "==> Creating main worktree at ${MAIN_ROOT}"
  git worktree add -B main "${MAIN_ROOT}" origin/main
fi

write_launcher() {
  local path="$1"
  local label="$2"
  local root="$3"
  local app="$4"
  local branch="$5"
  cat >"${path}" <<EOF
#!/bin/bash
set -euo pipefail

ROOT="${root}"
APP="${app}"
BRANCH="${branch}"
LAUNCHER="${label}"

if [[ ! -f "\${APP}/installer/run_pigeon_0_7.command" ]]; then
  osascript -e "display alert \"Pigeon not found\" message \"Expected install at:\${APP}\" as critical"
  exit 1
fi

cd "\${ROOT}" || exit 1
VERSION="unknown"
VP="\${APP}/pigeonSystem/pigeon/version.py"
if [[ -f "\${VP}" ]]; then
  VERSION="\$(grep -E '^PATCH = ' "\${VP}" | head -1 | sed 's/PATCH = //')"
  MINOR="\$(grep -E '^MINOR = ' "\${VP}" | head -1 | sed 's/MINOR = //')"
  MAJOR="\$(grep -E '^MAJOR = ' "\${VP}" | head -1 | sed 's/MAJOR = //')"
  VERSION="\${MAJOR}.\${MINOR}.\${VERSION}"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Pigeon — \${LAUNCHER}"
echo "  branch \${BRANCH}  ·  v\${VERSION}"
echo "  \${APP}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exec bash "\${APP}/installer/run_pigeon_0_7.command"
EOF
  chmod +x "${path}"
  echo "==> Wrote ${path}"
}

write_launcher "${LAUNCHER_EXPERIMENT}" "experiment" "${EXPERIMENT_ROOT}" "${EXPERIMENT_APP}" "experiment"
write_launcher "${LAUNCHER_MAIN}" "main (release)" "${MAIN_ROOT}" "${MAIN_APP}" "main"

echo
echo "Done. Double-click on your Desktop:"
echo "  • Pigeon (experiment).command  — latest dev branch"
echo "  • Pigeon (main).command        — same code the Updates button ships"
echo
echo "Re-run this script anytime to refresh the main worktree or recreate launchers."
