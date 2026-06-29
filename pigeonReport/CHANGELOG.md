# Pigeon Changelog

## 0.6.1 - 2026-04-16 07:38:29 EDT

- **Date/Time:** 2026-04-16 07:38:29 EDT
- **Changes vs previous build (0.6.0):**
  - Fixed Settings footer crash introduced during version-label work.
  - Completed requested folder naming/containment (`pigeonSystem`, `pigeonCashe`) and moved `testingEnvironments` to the development folder.
  - Smoothed splash playback by caching and prefetching frames; fixed a splash cache bug that caused a black screen.
  - Extended the visualizer “float off” descent duration (slower exit).
- **Total lines of code across Pigeon files:** 23011 (46 Python files)
- **Bugs:** Pending post-build test pass.

## 0.6.0 - 2026-04-15 12:32:01 EDT

- **Date/Time:** 2026-04-15 12:32:01 EDT
- **Changes vs previous build (0.5.0):**
  - Promoted versioning to semantic format and started the `0.6.0` line.
  - Migrated desktop project layout into `Pigeon_0.6.0_Development/Pigeon_0.6.0` with archived `0.5.0`.
  - Added version display to the Settings page footer.
  - Updated splash sequence path to `pigeonAssets/pigeonSplash` (with legacy fallback support).
  - Reworked startup visualizer motion into a 3-second sequence: float on -> flap -> ascend -> soar -> float off.
  - Updated TMDB folder protocol to use `pigeonTMDB_ORIGINAL`, `pigeonTMDB_BD`, and `pigeonTMDB_TT` with 20-file caps on BD/TT and automatic ORIGINAL purge before new fetches.
  - Added a new build README for `Pigeon_0.6.0`.
- **Total lines of code across Pigeon files:** 23131 (47 Python files)
- **Bugs:** Pending post-build test pass.
