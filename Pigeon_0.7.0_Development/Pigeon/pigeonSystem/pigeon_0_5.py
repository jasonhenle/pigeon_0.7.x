"""Backward-compatible launcher shim.

Some local launch scripts may still point at ``pigeon_0_5.py``. Keep this tiny
shim so both filenames start the same app entrypoint.
"""

from pigeon_0_7 import main


if __name__ == "__main__":
    raise SystemExit(main())
import argparse
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

# Tk must initialize before OpenCV on macOS: cv2 pulls X11/SDL dylibs that otherwise break Tcl (Tcl_InitNotifier → abort).
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import tkinter.scrolledtext as scrolledtext
import tkinter.simpledialog as simpledialog

import numpy as np
from PIL import Image, ImageTk
import cv2

# Status bar: one black pill cols 3–17, bar-shaped mask hole + translucent bar; rows 6–8 gradient.
# Remaining label spans cols 16–17 (was 17–18) so the right edge has one extra column of margin.
TRT_DISPLAY_ROW = 8.0  # nowPlaying band bottom aligns to row 9 (baseline of canvas)
TRT_PLAYED_COL = 3  # TRTPlayed (elapsed)
TRT_PLAYED_TEXT = "0"
TRT_REMAINING_COL = 16  # TRTRemaining (countdown); 2-wide → ends at col 17 (col 19 = breathing room)
TRT_REMAINING_TEXT = "1:00:00"
TRT_LABEL_SPAN_W = 2
TRT_LABEL_SPAN_H = 1
# Apple TV auto-poll; TRT labels on a steady ~1 Hz metronome (see _playback_ui_tick).
APPLE_TV_POLL_MS = 3000
RECEIVER_POLL_MS = 750
APPLE_TV_IDLE_POLL_MS = 6000
APPLE_TV_FAIL_POLL_MAX_MS = 15000
PLAYBACK_UI_TICK_MS = 1000  # fallback first delay only; actual spacing uses monotonic deadlines
# Title / TMDb logo (views 1/3/5; not view 2 visualizer): 5×2 top-right at grid (1.5, 13); top-aligned in cell.
TMDB_LOGO_ANCHOR_ROW = 1.5
TMDB_LOGO_TOP_RIGHT_COL = 13.0
TMDB_LOGO_SPAN_W = 5
TMDB_LOGO_SPAN_H = 2
TMDB_LOGO_FIT_SCALE = 0.88
# View 6 logo constraints: grow as large as possible while staying inside rows 2..5 and cols 2..18.
TMDB_LOGO_VIEW6_ANCHOR_ROW = 2
TMDB_LOGO_VIEW6_ANCHOR_COL = 2
TMDB_LOGO_VIEW6_SPAN_W = 17
TMDB_LOGO_VIEW6_SPAN_H = 4
TMDB_LOGO_VIEW6_FIT_SCALE = 1.0
VIEW_ONE_BADGE_COL_RIGHT = 4.0  # 2-wide badge left-aligns to grid column 2
VIEW_ONE_CLOCK_COL_RIGHT = 19.0  # clock right edge aligns to the right side of column 18
_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

_PROJECT_DIR = _CODE_DIR
if not os.path.isdir(os.path.join(_PROJECT_DIR, "pigeonAssets")):
    _parent = os.path.dirname(_PROJECT_DIR)
    if os.path.isdir(os.path.join(_parent, "pigeonAssets")):
        _PROJECT_DIR = _parent

# One-time migration: stale installs still import ``mic_wave_visualizer`` (broken on Py 3.14+).
_boot_script = Path(__file__).resolve()
_boot_dir = _boot_script.parent
_legacy_viz = _boot_dir / "pigeon" / "mic_wave_visualizer.py"
if _legacy_viz.is_file():
    try:
        _legacy_viz.unlink()
    except OSError:
        pass
try:
    _boot_src = _boot_script.read_text(encoding="utf-8")
except OSError:
    _boot_src = ""
# Build old import without embedding it verbatim — otherwise ``_import_old in _boot_src`` matches this file.
_old_mod = "mic_wave_visualizer"
_import_old = f"from pigeon.{_old_mod} import blend_mic_visualizer"
_import_new = "from pigeon.audio_waves import blend_mic_visualizer"
if _import_old in _boot_src:
    _waves = _boot_dir / "pigeon" / "audio_waves.py"
    if not _waves.is_file():
        sys.stderr.write(
            "pigeon: missing pigeon/audio_waves.py — copy it from the repo (mic_wave_visualizer was removed).\n"
        )
        sys.stderr.flush()
        sys.exit(1)
    try:
        _boot_script.write_text(_boot_src.replace(_import_old, _import_new), encoding="utf-8")
        os.execv(sys.executable, [sys.executable, str(_boot_script), *sys.argv[1:]])
    except OSError:
        pass

from pigeon.app_state import (
    LOCATION_PRESET_ROOM_NAMES,
    add_empty_location_v2,
    append_device_to_location_slot,
    clear_all_persisted_devices_and_targets,
    clear_last_apple_tv,
    clear_last_receiver,
    merge_legacy_saved_receivers_into_av_slot,
    migrate_device_slots_from_legacy_if_needed,
    read_all_locations_v2,
    delete_location_v2,
    rename_location_v2,
    read_app_state,
    read_current_location_id,
    read_last_apple_tv,
    read_last_receiver,
    read_saved_av_receiver,
    read_saved_streaming_device,
    read_saved_streaming_devices_all,
    remove_device_at_slot_index,
    write_saved_game,
    write_saved_other,
    write_saved_projector,
    write_saved_tv,
    row_is_playback_apple_tv,
    set_current_location_id,
    write_app_state,
    write_last_apple_tv,
    write_last_receiver,
    write_saved_av_receiver,
    write_saved_streaming_device,
    advance_delegation_active,
    append_delegation_log_line,
)
from pigeon.media_folders import (
    consolidate_legacy_pigeondata_media_folders,
    pigeon_pulled_media_dir,
    pigeon_reformatted_media_dir,
    purge_directory_contents,
)
from pigeon.compositing import cv_resize_interp
from pigeon.stage_background import bgr_to_tk_hex, get_stage_bgr, set_stage_bgr
from pigeon.tmdb_tt_contrast import GRADIENT_BGR_DARK, pick_gradient_bgr
from pigeon.startup_transition_video import current_startup_bgra_frame
from pigeon.runtime_paths import PIGEON_STATE_DIR_TILDE, pigeon_state_dir
from pigeon.version import version_string

try:
    from pigeon.tmdb_retry_log import append_entry as _tmdb_retry_log_append
    from pigeon.tmdb_retry_log import read_tail_lines as _tmdb_retry_log_read_tail
except ImportError:

    def _tmdb_retry_log_append(_entry: dict) -> None:
        pass

    def _tmdb_retry_log_read_tail(_max_lines: int = 120) -> list[str]:
        return []


try:
    from pigeon.compositing import (
        alpha_blend_bgra_over_bgr,
        lerp_bgr_red_monochrome,
        scale_bgra_rgb,
        scale_cover_center_crop,
        scale_height_and_center_crop,
        scale_uniform_letterbox,
    )
    from pigeon.design import (
        DESIGN_H,
        DESIGN_W,
        get_grid_geometry,
        playback_lower_gradient_bgra,
        rect_for_span_at_cell,
        rect_for_span_top_right_at_cell,
    )
    from pigeon.overlay import blend_overlay_bgr, build_stage_overlay_source_bgra
    from pigeon.widgets.clock_calendar import (
        CLOCK_WIDGET_COL_RIGHT,
        CLOCK_WIDGET_ROW,
        ClockCalendarWidget,
    )
    CLOCK_ANCHOR_ROW = CLOCK_WIDGET_ROW
    CLOCK_ANCHOR_COL = int(VIEW_ONE_CLOCK_COL_RIGHT)
    from pigeon.widgets.location_toast import (
        LOCATION_TOAST_FADE_S,
        LOCATION_TOAST_FULL_S,
        location_toast_patch_bgra,
    )
    from pigeon.widgets.logo_tmdb import TmdbLogoWidget
    from pigeon.view_one_variants import (
        ViewOneVariant,
        load_pigeon_temp_logo_bgra,
        render_ui_music_text_patch_bgra,
        render_ui_text_patch_bgra,
        render_view_one_video_content_b_title_patch_bgra,
        resolve_view_one_variant,
        variant_has_alternate,
        variant_uses_full_path,
    )
    from pigeon.widgets.status_bar import StatusBarWidget
    from pigeon.widgets.clock_saver import clock_saver_composite_bgra
    from pigeon.widgets.playback_overlay import (
        PATCH_LAYER_STREAMING_BADGE,
        PlaybackOverlayWidget,
        pigeon_wordmark_design_patch,
    )
    from pigeon.widgets.poster_art import prepare_default_poster_at_startup
    from pigeon.splash_sequence import (
        FALLBACK_SPLASH_FRAME_COUNT,
        SPLASH_FADE_OUT_FRAMES,
        SPLASH_FPS,
        SPLASH_MAX_DURATION_S,
        apply_splash_global_alpha,
        bgra_to_pil_rgba,
        builtin_splash_bgra_frame,
        composite_splash_over_bg,
        find_splash_video_path,
        flatten_bgra_over_bg_to_rgb,
        list_splash_png_paths,
        load_splash_bgra,
        resize_bgra_if_needed,
        splash_end_fade_factor,
    )

    try:
        from pigeon.audio_waves import (
            MIC_VIZ_INTRO_TOTAL_S,
            MIC_VIZ_LAUNCH_DESCENT_S,
            blend_mic_visualizer as _blend_mic_visualizer,
        )
    except ImportError:
        MIC_VIZ_INTRO_TOTAL_S = 1.0  # type: ignore[misc, assignment]
        MIC_VIZ_LAUNCH_DESCENT_S = 0.62  # type: ignore[misc, assignment]
        _blend_mic_visualizer = None  # type: ignore[misc, assignment]

    _PIGEON_EXT = True
except ImportError:
    alpha_blend_bgra_over_bgr = None  # type: ignore[misc, assignment]
    lerp_bgr_red_monochrome = None  # type: ignore[misc, assignment]
    scale_bgra_rgb = None  # type: ignore[misc, assignment]
    scale_height_and_center_crop = None  # type: ignore[misc, assignment]
    scale_cover_center_crop = None  # type: ignore[misc, assignment]
    scale_uniform_letterbox = None  # type: ignore[misc, assignment]
    rect_for_span_at_cell = None  # type: ignore[misc, assignment]
    rect_for_span_top_right_at_cell = None  # type: ignore[misc, assignment]
    get_grid_geometry = None  # type: ignore[misc, assignment]
    playback_lower_gradient_bgra = None  # type: ignore[misc, assignment]
    DESIGN_W = DESIGN_H = 0
    blend_overlay_bgr = None  # type: ignore[misc, assignment]
    build_stage_overlay_source_bgra = None  # type: ignore[misc, assignment]
    prepare_default_poster_at_startup = None  # type: ignore[misc, assignment]
    ClockCalendarWidget = None  # type: ignore[misc, assignment]
    TmdbLogoWidget = None  # type: ignore[misc, assignment]
    StatusBarWidget = None  # type: ignore[misc, assignment]
    PlaybackOverlayWidget = None  # type: ignore[misc, assignment]
    PATCH_LAYER_STREAMING_BADGE = "streaming_badge"  # type: ignore[misc, assignment]
    clock_saver_composite_bgra = None  # type: ignore[misc, assignment]
    pigeon_wordmark_design_patch = None  # type: ignore[misc, assignment]
    LOCATION_TOAST_FULL_S = 15.0
    LOCATION_TOAST_FADE_S = 2.0
    location_toast_patch_bgra = None  # type: ignore[misc, assignment]
    _blend_mic_visualizer = None  # type: ignore[misc, assignment]
    ViewOneVariant = None  # type: ignore[misc, assignment]
    resolve_view_one_variant = None  # type: ignore[misc, assignment]
    variant_has_alternate = None  # type: ignore[misc, assignment]
    variant_uses_full_path = None  # type: ignore[misc, assignment]
    render_ui_text_patch_bgra = None  # type: ignore[misc, assignment]
    render_view_one_video_content_b_title_patch_bgra = None  # type: ignore[misc, assignment]
    render_ui_music_text_patch_bgra = None  # type: ignore[misc, assignment]
    load_pigeon_temp_logo_bgra = None  # type: ignore[misc, assignment]
    _PIGEON_EXT = False


# Default Tk geometry: 800×480 (5:3), ~30% larger than the native Pi panel size.
_LAUNCH_WINDOW_SCALE = 1.3
WINDOW_W = int(round(800 * _LAUNCH_WINDOW_SCALE))
WINDOW_H = int(round(480 * _LAUNCH_WINDOW_SCALE))


def _resize_bgr_to_dims(dst_w: int, dst_h: int, src: np.ndarray) -> np.ndarray:
    """Resize to window/cap size; LANCZOS4 when upscaling, AREA when downscaling."""
    sh, sw = int(src.shape[0]), int(src.shape[1])
    if sw == dst_w and sh == dst_h:
        return src
    return cv2.resize(
        src,
        (dst_w, dst_h),
        interpolation=cv_resize_interp(sw, sh, dst_w, dst_h),
    )


def _composite_cap_dims(display_w: int, display_h: int) -> tuple[int, int, bool]:
    """
    Internal composite size for video + mic EQ + UI blits: fit inside WINDOW_W×WINDOW_H
    while preserving the live window aspect, then upscale once for Tk. Avoids huge buffers
    when the window is resized (previously only width was capped).
    """
    dw = max(1, int(display_w))
    dh = max(1, int(display_h))
    s = min(WINDOW_W / float(dw), WINDOW_H / float(dh))
    if s >= 1.0:
        return dw, dh, False
    cap_w = max(1, int(round(dw * s)))
    cap_h = max(1, int(round(dh * s)))
    return cap_w, cap_h, True


# App-logo backdrop when TMDb has no art: letterbox canvas is at most this fraction of the live window.
APP_LOGO_FALLBACK_MAX_RESOLUTION_FRACTION = 0.9
# Reserved strip at bottom when developer mode is on (must not sit under the full-bleed video label).
OVERLAY_HUD_H = 52


class DevPhase(IntEnum):
    OFF = 0
    GRID = 1
    SETTINGS = 2


class DisplayView(IntEnum):
    """User-selectable display layout (keys 1–6)."""

    ONE = 1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6


class ViewOneLayout(IntEnum):
    """
    When ``DisplayView.ONE`` is active, key ``1`` cycles the layout toggle.

    Member names are **historical** — they describe the happy-path visual that
    shipped before the v0.6.14 V01 ↔ V02 swap and the v0.6.19 view-naming
    refactor. The toggle's real meaning in the new vocabulary is
    ``viewOne.videoContent_a`` (initial) vs. ``viewOne.videoContent_b``
    (alternate). The V# variant that actually renders depends on this toggle
    *and* on which content assets are live (see
    ``pigeon.view_one_variants.resolve_view_one_variant``).

    ``PIGEON_FULL`` (0, initial) → ``viewOne.videoContent_a``: default mode.
    When all TMDb assets are present renders V01 — minimal pigeonTMDB_TT on
    black. With missing assets routes to V03 / V05 / V07 / V08 / V09.
    ``PIGEON_SIMPLE`` (1) → ``viewOne.videoContent_b``: alternate mode.
    Toggled on by pressing [``1``] while on view 1. When all TMDb assets are
    present renders V02 — the full pigeonTMDB_BD + pigeonTMDB_TT + appLogo
    composition. With missing assets routes to V04 / V06.

    ``PIGEON_POSTER`` (2) → ``viewOne.videoContent_c``: poster mode. Keeps the
    same chrome stack as viewOne.videoContent_b but replaces TMDb TT/BD with
    ``pigeonTMDB_Poster`` centered horizontally in the top 7 rows.

    Related view identifiers in the new vocabulary (not modeled by this enum
    because they're driven by content presence or idle timers rather than a
    user toggle):

    * ``viewOne.audioContent`` — music-only content. A MediaType.Music
      override in ``_compose_shown_frame`` substitutes a two-line text patch
      ("Track title" + "Artist - Album") inside the pigeonTMDB_TT rect and
      suppresses the bottom gradient.
    * ``viewOne.startup`` / ``viewOne.noContent`` — V09 fallback; the Pigeon
      appLogo renders at 30% opacity over a black scene.
    * ``startUp.transition`` — the post-splash bars/bird choreography window;
      V09's logo slot shows a looping frame of ``pigeonStartup.mp4`` until
      ``MIC_VIZ_INTRO_TOTAL_S`` elapses, then falls back to the 30% logo.
    * ``viewOne.clockSaver_a`` / ``viewOne.clockSaver_b`` — the idle clock
      saver composites over whichever base (black or pigeonTMDB_BD) the
      active videoContent variant produced.
    * ``viewTwo.clock`` — the full-screen clock (``DisplayView.THREE`` under
      the hood). Pressing [``2``] now routes here; the old visualizer-only
      ``DisplayView.TWO`` path is deprecated.
    """

    PIGEON_FULL = 0
    PIGEON_SIMPLE = 1
    PIGEON_POSTER = 2


# TMDb static backdrop scene (not paused-video dim 0.3).
BACKDROP_BRIGHTNESS = 0.8
# Static landing logo (no video): full brightness; old 0.3 “paused video” level hid the art.
LANDING_DISPLAY_BRIGHTNESS = 1.0
LANDING_DIM_BRIGHTNESS = 0.78  # Space-bar pulse “off” — still readable vs old 0.3
# After UI bootstrap, optional auto-restore of saved TMDb backdrop (env-gated) runs after this delay.
STARTUP_PIGEON_WORDMARK_MAX_S = 5.0
# If True and a saved TMDb backdrop exists, switch to it when this timer elapses (enables ``use_backdrop_scene``,
# which turns off the mic EQ). Default False so landing + clock saver + EQ stay on until you use F10 / Space (saved backdrop).
STARTUP_AUTO_RESTORE_SAVED_BACKDROP = os.environ.get("PIGEON_STARTUP_RESTORE_BACKDROP", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# Theater UI: red luma-mono only when neither Apple TV nor Pigeon UI shows activity this long.
# ATV side: poll metadata deltas (see _update_atv_interaction_from_poll_metadata).
# Pigeon side: mouse / keyboard / settings scroll (see _bump_pigeon_user_activity).
# For ATV, 0 = never bumped → treated as idle until the first remote-driven signal (still needs Pigeon idle).
THEATER_IDLE_DIM_AFTER_S = 45.0
# Red idle-dim is **off** by default until an ambient-light (or other) trigger is integrated.
# Re-enable anytime: ``PIGEON_THEATER_IDLE_DIM=1`` (or ``true`` / ``yes`` / ``on``). Explicit ``0`` / ``off`` keeps it off.
_THEATER_IDLE_DIM_ENV = os.environ.get("PIGEON_THEATER_IDLE_DIM", "").strip().lower()
THEATER_IDLE_DIM_ENABLED = (
    _THEATER_IDLE_DIM_ENV in ("1", "true", "yes", "on")
    if _THEATER_IDLE_DIM_ENV
    else False
)
# Ease in/out between full color and red luma-mono when idle-dim target changes.
ATV_IDLE_MONO_ANIM_S = 2.0
# Idle “clock saver” (screensaver): requires both ``CLOCK_SAVER_AFTER_S`` of **UI** inactivity (mouse/keys/etc.;
# mic does not touch those timestamps) **and** the same span without **significant** device signals (content
# selection, ``device_state``, or ``volume_percent`` from the player poll, plus receiver volume line changes).
CLOCK_SAVER_AFTER_S = 300.0
# Saver text opacity when idle; tap while saver is up briefly uses 1.0 (see clock_saver_peek_until_mono).
CLOCK_SAVER_DIM_OPACITY = 0.54
CLOCK_SAVER_PEEK_S = 2.5
# With a TMDb backdrop visible: fade backdrop to black under the clock after this idle span on the saver.
# Clock saver: multiply backdrop RGB by this factor (0.3 → 30% brightness) while saver is active.
CLOCK_SAVER_BACKDROP_DIM = 0.3
# Position-stall grace: while content position has advanced within this window, the clock-saver
# "device signal" timestamp keeps being bumped to ``now``, so the 300 s saver timer stays reset.
# Once the reported playback position stays flat for longer than this, the timer resumes counting
# from the last advance toward ``CLOCK_SAVER_AFTER_S``. Tuned to survive short buffering stalls
# (typical re-buffer is < 2 s) without prematurely arming the saver. A position advance while the
# saver is already open ends it on the next render tick via the same bump.
CLOCK_SAVER_POSITION_STALL_GRACE_S = 5.0

# Idle (not actively playing) mic visualizer updates do not need 30 FPS.
# Lowering this cadence significantly reduces cv2/Tk upload churn while the
# ambient bars remain visually smooth.
MIC_VIZ_IDLE_COMPOSITE_MS = 83  # ~12 FPS

HOTKEY_BINDTAG = "Pigeon0_5_hotkeys"

def _paint_boolean_led(canvas: tk.Canvas, ok: bool | None) -> None:
    """Single lamp: green=detected+compatible, amber=detected+unknown/incompatible, red=not detected/not compatible."""
    canvas.delete("all")
    if ok is True:
        fill = "#1fcb5d"
    elif ok is None:
        fill = "#f0ad4e"
    else:
        fill = "#e74c3c"
    canvas.create_oval(2, 2, 14, 14, fill=fill, outline="#151518", width=1)


def _prepend_hotkey_bindtag(widget: tk.Misc, tag: str = HOTKEY_BINDTAG) -> None:
    widget.bindtags((tag,) + widget.bindtags())


def _widget_accepts_typing(widget: tk.Misc) -> bool:
    """True if Return/other keys should go to the widget (not global developer-mode shortcuts)."""
    try:
        cls = widget.winfo_class()
    except tk.TclError:
        return False
    if cls == "Text":
        try:
            if str(widget.cget("state")).lower() == "disabled":
                return False
        except tk.TclError:
            pass
        return True
    if cls in ("Entry",):
        return True
    if cls == "TEntry" or cls == "TCombobox":
        return True
    return False


def _alternate_tmdb_query_from_metadata(md: dict | None, primary: str) -> str | None:
    """
    Pick a different TMDb query from last Apple TV metadata.

    Order prefers ``series_name`` / ``artist`` before ``title`` (title is often ``Sketch - SNL``).
    Refined strings that mean the same show as the primary (e.g. ``SNL`` vs ``Papyrus - SNL``)
    are skipped so ``?`` retry does not replace a good match with a sketch line.
    """
    if not primary or not md:
        return None
    try:
        from pigeon.tmdb_poster import equivalent_tmdb_search_queries, refine_tmdb_search_query
    except ImportError:

        def refine_tmdb_search_query(x: str | None) -> str | None:  # type: ignore[misc]
            return (str(x).strip() or None) if x else None

        def equivalent_tmdb_search_queries(a: str, b: str) -> bool:  # type: ignore[misc]
            return (a or "").strip().lower() == (b or "").strip().lower()

    pr = refine_tmdb_search_query(primary.strip()) or primary.strip()
    for key in ("series_name", "artist", "album", "title"):
        c = str(md.get(key) or "").strip()
        if not c:
            continue
        cr = refine_tmdb_search_query(c) or c
        if equivalent_tmdb_search_queries(cr, pr):
            continue
        return cr
    return None


def _pyatv_install_hint() -> str:
    exe = sys.executable or "python3"
    return f"Apple TV support requires pyatv.\n\nInstall with:\n  {exe} -m pip install pyatv"


@dataclass(frozen=True)
class SceneFit:
    target_w: int = WINDOW_W
    target_h: int = WINDOW_H

    def scale_and_crop(self, frame_bgr: np.ndarray) -> np.ndarray:
        if frame_bgr is None or frame_bgr.size == 0:
            raise ValueError("Empty frame")

        src_h, src_w = frame_bgr.shape[:2]
        if src_h <= 0 or src_w <= 0:
            raise ValueError(f"Invalid frame size {src_w}x{src_h}")

        scale = self.target_h / float(src_h)
        scaled_w = int(round(src_w * scale))
        scaled_h = self.target_h

        resized = cv2.resize(
            frame_bgr,
            (scaled_w, scaled_h),
            interpolation=cv_resize_interp(src_w, src_h, scaled_w, scaled_h),
        )

        if scaled_w == self.target_w:
            return resized

        if scaled_w < self.target_w:
            pad = self.target_w - scaled_w
            left = pad // 2
            right = pad - left
            sb, sg, sr = get_stage_bgr()
            return cv2.copyMakeBorder(
                resized,
                top=0,
                bottom=0,
                left=left,
                right=right,
                borderType=cv2.BORDER_CONSTANT,
                value=(sb, sg, sr),
            )

        x0 = (scaled_w - self.target_w) // 2
        x1 = x0 + self.target_w
        return resized[:, x0:x1]


def _default_render_fps() -> float:
    """Tk timer cadence for static landing + composited widgets (no scene video)."""
    return 30.0


def _build_landing_design_bgr(design_w: int, design_h: int, logo_path: Path | None) -> np.ndarray:
    """Pure black design-sized canvas with the Pigeon logo centered (alpha-aware)."""
    out = np.zeros((design_h, design_w, 3), dtype=np.uint8)
    if logo_path is None or not logo_path.is_file():
        sys.stderr.write("pigeon: landing logo not found — using black screen only\n")
        sys.stderr.flush()
        return out
    arr: np.ndarray | None = None
    try:
        arr = cv2.imread(str(logo_path), cv2.IMREAD_UNCHANGED)
    except Exception:
        arr = None
    if arr is None or arr.size == 0:
        try:
            pil_img = Image.open(logo_path).convert("RGBA")
            arr = np.asarray(pil_img, dtype=np.uint8)
        except Exception as e:
            sys.stderr.write(f"pigeon: could not load landing logo {logo_path}: {e}\n")
            sys.stderr.flush()
            return out
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    rh, rw = int(arr.shape[0]), int(arr.shape[1])
    if rw <= 0 or rh <= 0:
        return out
    max_side = int(0.55 * min(design_w, design_h))
    max_side = max(32, max_side)
    scale = min(max_side / float(rw), max_side / float(rh), 1.0)
    nw = max(1, int(round(rw * scale)))
    nh = max(1, int(round(rh * scale)))
    if nw != rw or nh != rh:
        arr = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
    rh, rw, ch = int(arr.shape[0]), int(arr.shape[1]), int(arr.shape[2])
    if ch >= 4:
        bgr = arr[:, :, :3]
        alpha = arr[:, :, 3:4].astype(np.float32) / 255.0
    else:
        bgr = arr[:, :, :3]
        alpha = np.ones((rh, rw, 1), dtype=np.float32)
    x0 = (design_w - nw) // 2
    y0 = (design_h - nh) // 2
    roi = out[y0 : y0 + nh, x0 : x0 + nw]
    roi[:] = (bgr.astype(np.float32) * alpha + roi.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)
    return out


def _apply_brightness(frame_bgr: np.ndarray, factor: float) -> np.ndarray:
    factor = float(factor)
    if factor >= 0.999:
        return frame_bgr
    if factor <= 0.0:
        return np.zeros_like(frame_bgr)
    return cv2.convertScaleAbs(frame_bgr, alpha=factor, beta=0)


def _bgr_to_tk_image(frame_bgr: np.ndarray) -> ImageTk.PhotoImage:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)
    return ImageTk.PhotoImage(image=img)


def _update_label_photo_from_bgr(
    label: tk.Label,
    frame_bgr: np.ndarray,
    holder: list[ImageTk.PhotoImage | None],
) -> None:
    """
    Reuse one ``PhotoImage`` and ``paste`` each frame. Creating hundreds of new
    ``PhotoImage`` objects per second leaks native Tk storage and locks up after a short run.
    """
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 1 or w < 1:
        return
    pil_img = Image.fromarray(rgb)
    ph = holder[0]
    try:
        if ph is None or ph.width() != w or ph.height() != h:
            holder[0] = ImageTk.PhotoImage(image=pil_img)
            ph = holder[0]
        else:
            ph.paste(pil_img)
    except tk.TclError:
        holder[0] = ImageTk.PhotoImage(image=pil_img)
        ph = holder[0]
    # Re-configuring the same PhotoImage every tick still stresses Tk; paste updates pixels in place.
    if getattr(label, "image", None) is not ph:
        label.configure(image=ph)
    label.image = ph


def _load_persisted_scene_enabled(default: bool = True) -> bool:
    v = read_app_state().get("scene_enabled")
    if isinstance(v, bool):
        return v
    return default


def _save_persisted_scene_enabled(enabled: bool) -> None:
    write_app_state(scene_enabled=enabled)


def _format_hmmss(seconds_value: float | int | None) -> str:
    """Compact clock for on-screen TRT: drop leading zeros / unused fields (e.g. ``30:00``, ``59:29``)."""
    try:
        total_seconds = max(0, int(float(seconds_value or 0)))
    except (TypeError, ValueError):
        total_seconds = 0
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    if minutes > 0:
        return f"{minutes}:{seconds:02d}"
    return f"{seconds}"


def main() -> int:
    sys.stderr.write(f"pigeon: running script {os.path.abspath(__file__)}\n")
    sys.stderr.flush()

    parser = argparse.ArgumentParser(prog=f"Pigeon {version_string()}", add_help=True)
    parser.parse_args()

    cap: cv2.VideoCapture | None = None

    root = tk.Tk()
    root.title("")
    root.geometry(f"{WINDOW_W}x{WINDOW_H}")
    root.minsize(int(round(400 * _LAUNCH_WINDOW_SCALE)), int(round(240 * _LAUNCH_WINDOW_SCALE)))
    root.resizable(True, True)
    try:
        root.wm_aspect(5, 3, 5, 3)
    except tk.TclError:
        pass
    root.protocol("WM_DELETE_WINDOW", root.quit)
    # Ensure unexpected Tk callback errors are surfaced (and don't silently kill UI behavior).
    def _report_callback_exception(exc, val, tb) -> None:  # type: ignore[no-untyped-def]
        # Tk calls this as report_callback_exception(exc, val, tb) — no bound self.
        import traceback

        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            sys.stderr.write("pigeon: Tk callback exception\n" + text + "\n")
            sys.stderr.flush()
        except Exception:
            pass
        try:
            messagebox.showerror("Pigeon error", text)
        except Exception:
            pass

    root.report_callback_exception = _report_callback_exception  # type: ignore[method-assign]

    shell = tk.Frame(root, bg="#111")
    shell.pack(fill=tk.BOTH, expand=True)
    # Main UI is built here; optional full-window splash overlay sits above until bootstrap + animation finish.
    content_host = tk.Frame(shell, bg="#111")
    content_host.pack(fill=tk.BOTH, expand=True)

    # Full-window splash when extensions load: H.264/HEVC video if present (hardware-decoded on
    # macOS via VideoToolbox), else PNG sequence, else built-in wordmark.
    startup_ph: list[tk.Widget | None] = [None]
    splash_png_paths: list[Path] = []
    splash_video_path: Path | None = None
    if _PIGEON_EXT:
        try:
            _assets_root = Path(_PROJECT_DIR) / "pigeonAssets"
            splash_video_path = find_splash_video_path(_assets_root)
            if splash_video_path is None:
                splash_png_paths = list_splash_png_paths(_assets_root)
        except Exception:
            splash_png_paths = []
            splash_video_path = None

    bootstrap_done: list[bool] = [False]
    splash_anim_done: list[bool] = [False]
    # Mic EQ intro (bars rising) uses this as t0 so animation starts when splash lifts, not at UI build.
    mic_viz_intro_start_mono: list[float | None] = [None]
    # After ``MIC_VIZ_INTRO_TOTAL_S``: ``None`` until latched; ``1`` = backdrop was on → descend EQ; ``0`` = leave EQ up.
    mic_viz_launch_descend_latched: list[int | None] = [None]

    def _try_remove_splash_overlay() -> None:
        if not _PIGEON_EXT:
            return
        if not (bootstrap_done[0] and splash_anim_done[0]):
            return
        w = startup_ph[0]
        if w is None:
            return
        try:
            w.destroy()
        except tk.TclError:
            pass
        startup_ph[0] = None
        if mic_viz_intro_start_mono[0] is None:
            mic_viz_intro_start_mono[0] = time.monotonic()

    _tk_pack_orig = tk.Widget.pack
    _tk_grid_orig = tk.Widget.grid
    _tk_place_orig = tk.Widget.place
    _splash_pump_next: list[float] = [0.0]

    def _splash_pump_maybe() -> None:
        if not _PIGEON_EXT or bootstrap_done[0]:
            return
        now = time.monotonic()
        if now < _splash_pump_next[0]:
            return
        _splash_pump_next[0] = now + (1.0 / 30.0)
        try:
            root.update()
        except tk.TclError:
            pass

    def _pack_patched(self: tk.Misc, *args: object, **kwargs: object) -> object | None:
        r = _tk_pack_orig(self, *args, **kwargs)
        _splash_pump_maybe()
        return r

    def _grid_patched(self: tk.Misc, *args: object, **kwargs: object) -> object | None:
        r = _tk_grid_orig(self, *args, **kwargs)
        _splash_pump_maybe()
        return r

    def _place_patched(self: tk.Misc, *args: object, **kwargs: object) -> object | None:
        r = _tk_place_orig(self, *args, **kwargs)
        _splash_pump_maybe()
        return r

    if _PIGEON_EXT:
        # Stay a direct child of ``shell`` (placed full-size). Do **not** pack into ``video_area`` after
        # the video ``Label``: two ``pack(..., fill=BOTH, expand=True)`` siblings leave the second with
        # zero height, so the splash would disappear. Transparent PNG pixels still show ``content_host``.
        splash_overlay = tk.Frame(shell, bg="#000", highlightthickness=0, bd=0)
        splash_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        # Placed widgets can sit under later-packed siblings (e.g. ``hud_bar``); pin above ``content_host``.
        try:
            splash_overlay.lift(content_host)
        except tk.TclError:
            try:
                splash_overlay.lift()
            except tk.TclError:
                pass
        startup_ph[0] = splash_overlay
        splash_label = tk.Label(splash_overlay, bg="#000", bd=0)
        splash_label.pack(expand=True, fill="both")
        splash_photo: list[ImageTk.PhotoImage | None] = [None]
        splash_idx = [0]
        splash_t0 = [time.monotonic()]
        _splash_bg_bgr = (0, 0, 0)

        # Resolve total frame count AND native fps up front. The PNG / built-in paths lock to
        # SPLASH_FPS, but a video drives its own cadence (e.g. 59.94) so the splash plays at
        # authored speed instead of being stretched or sped up by a hardcoded 30 Hz scheduler.
        _splash_fps_effective = float(max(1, SPLASH_FPS))
        if splash_video_path is not None:
            try:
                _probe = cv2.VideoCapture(str(splash_video_path))
                _vc_total = int(_probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                _vc_fps = float(_probe.get(cv2.CAP_PROP_FPS) or 0.0)
                _probe.release()
            except Exception:
                _vc_total = 0
                _vc_fps = 0.0
            splash_total_frames = max(1, _vc_total) if _vc_total > 0 else FALLBACK_SPLASH_FRAME_COUNT
            # Reject obviously-bogus fps values (VideoCapture sometimes returns 0 or 1000 on bad files).
            if 1.0 < _vc_fps < 240.0:
                _splash_fps_effective = _vc_fps
        elif splash_png_paths:
            splash_total_frames = len(splash_png_paths)
        else:
            splash_total_frames = FALLBACK_SPLASH_FRAME_COUNT

        frame_dt = 1.0 / _splash_fps_effective
        frame_ms = max(1, int(round(1000.0 * frame_dt)))

        # Cap by SPLASH_MAX_DURATION_S so a pathological asset can't block startup.
        _max_frames_for_duration = int(float(SPLASH_MAX_DURATION_S) * _splash_fps_effective)
        if _max_frames_for_duration > 0:
            splash_total_frames = min(splash_total_frames, _max_frames_for_duration)

        # Keep the reveal fade-out ~0.7 s regardless of source fps (21 frames @ 30 fps ≈ 0.7 s).
        _splash_fade_frames = max(
            1,
            int(round(float(SPLASH_FADE_OUT_FRAMES) * _splash_fps_effective / float(max(1, SPLASH_FPS)))),
        )
        _splash_fade_zone_start = max(
            0, splash_total_frames - min(_splash_fade_frames, splash_total_frames)
        )

        # Two parallel caches keyed by frame index:
        #   * _splash_rgb_cache: pre-flattened opaque RGB uint8 (WINDOW_H,WINDOW_W,3) for the
        #     non-fade body. Feeds PIL ``"RGB"`` which uses Tk's fast opaque blit path.
        #   * _splash_bgra_cache: BGRA uint8 (WINDOW_H,WINDOW_W,4) for the fade tail only, so
        #     per-tick we just multiply alpha and hand Tk a small, bounded number of RGBA frames.
        _splash_rgb_cache: dict[int, np.ndarray] = {}
        _splash_bgra_cache: dict[int, np.ndarray] = {}
        _splash_prebake_done = [False]
        _splash_video_cap_holder: list[cv2.VideoCapture | None] = [None]

        def _splash_raw_bgra(ii: int) -> np.ndarray | None:
            """Source-resolution BGRA for frame ``ii`` (PNG / built-in). Video path doesn't use this."""
            if splash_png_paths:
                if 0 <= ii < len(splash_png_paths):
                    return load_splash_bgra(splash_png_paths[ii])
                return None
            return builtin_splash_bgra_frame(
                ii, splash_total_frames, width=WINDOW_W, height=WINDOW_H
            )

        def _splash_store_prebaked(ii: int, bgra_window: np.ndarray) -> None:
            """Store a window-sized BGRA frame in the appropriate fast-path cache."""
            if ii < _splash_fade_zone_start:
                try:
                    rgb = flatten_bgra_over_bg_to_rgb(bgra_window, _splash_bg_bgr)
                except Exception:
                    _splash_bgra_cache[ii] = bgra_window
                    return
                _splash_rgb_cache[ii] = rgb
            else:
                _splash_bgra_cache[ii] = bgra_window

        def _splash_prebake_worker_pngs() -> None:
            """Decode + resize + flatten every PNG frame off the UI thread."""
            try:
                for ii in range(splash_total_frames):
                    if ii in _splash_rgb_cache or ii in _splash_bgra_cache:
                        continue
                    fr = _splash_raw_bgra(ii)
                    if fr is None:
                        continue
                    fr = resize_bgra_if_needed(fr, WINDOW_W, WINDOW_H)
                    _splash_store_prebaked(ii, fr)
            except Exception:
                pass
            finally:
                _splash_prebake_done[0] = True

        def _splash_prebake_worker_video() -> None:
            """Sequentially decode the splash video into the fast-path caches.

            Sequential reads on ``VideoCapture`` are hardware-accelerated on macOS
            (AVFoundation/VideoToolbox) and far cheaper than 100+ PNG decodes + resizes.
            """
            cap_v: cv2.VideoCapture | None = None
            try:
                cap_v = cv2.VideoCapture(str(splash_video_path))
                _splash_video_cap_holder[0] = cap_v
                if not cap_v.isOpened():
                    return
                for ii in range(splash_total_frames):
                    ok, bgr = cap_v.read()
                    if not ok or bgr is None:
                        break
                    if bgr.shape[1] != WINDOW_W or bgr.shape[0] != WINDOW_H:
                        _sw, _sh = int(bgr.shape[1]), int(bgr.shape[0])
                        bgr = cv2.resize(
                            bgr,
                            (WINDOW_W, WINDOW_H),
                            interpolation=cv_resize_interp(_sw, _sh, WINDOW_W, WINDOW_H),
                        )
                    if ii < _splash_fade_zone_start:
                        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                        _splash_rgb_cache[ii] = np.ascontiguousarray(rgb)
                    else:
                        # Fade-tail wants dynamic alpha, so synthesize opaque BGRA and let the
                        # tick multiply the alpha channel each frame.
                        h, w = bgr.shape[:2]
                        bgra = np.empty((h, w, 4), dtype=np.uint8)
                        bgra[:, :, :3] = bgr
                        bgra[:, :, 3] = 255
                        _splash_bgra_cache[ii] = bgra
            except Exception:
                pass
            finally:
                try:
                    if cap_v is not None:
                        cap_v.release()
                except Exception:
                    pass
                _splash_video_cap_holder[0] = None
                _splash_prebake_done[0] = True

        # Kick off the prebake thread immediately so frames are warm before ``splash_tick``
        # starts pulling from the cache post-``after_idle``.
        try:
            import threading

            _worker = _splash_prebake_worker_video if splash_video_path is not None else _splash_prebake_worker_pngs
            _splash_prebake_thread = threading.Thread(
                target=_worker, name="pigeon-splash-prebake", daemon=True
            )
            _splash_prebake_thread.start()
        except Exception:
            # Fall back to on-demand decode inside ``splash_tick``.
            _splash_prebake_done[0] = True

        def _splash_fallback_frame_sync(ii: int) -> np.ndarray | None:
            """On-demand decode if the worker hasn't populated this index yet (built-in only)."""
            if splash_video_path is not None:
                # We can't safely random-seek while the worker owns the VideoCapture.
                return None
            fr = _splash_raw_bgra(ii)
            if fr is None:
                return None
            fr = resize_bgra_if_needed(fr, WINDOW_W, WINDOW_H)
            _splash_store_prebaked(ii, fr)
            return fr

        def splash_tick() -> None:
            try:
                if not splash_label.winfo_exists():
                    return
            except tk.TclError:
                return
            ov_top = startup_ph[0]
            if ov_top is not None:
                try:
                    ov_top.lift()
                except tk.TclError:
                    pass
            ntot = splash_total_frames
            if time.monotonic() - splash_t0[0] > float(SPLASH_MAX_DURATION_S):
                splash_idx[0] = ntot
            i = splash_idx[0]
            if i >= ntot:
                splash_anim_done[0] = True
                _try_remove_splash_overlay()
                return

            rgb_hit = _splash_rgb_cache.get(i)
            bgra_hit = _splash_bgra_cache.get(i) if rgb_hit is None else None

            # Worker hasn't reached this index yet: try a short spin before giving up.
            if rgb_hit is None and bgra_hit is None:
                bgra_fb = _splash_fallback_frame_sync(i)
                if bgra_fb is not None:
                    if i < _splash_fade_zone_start:
                        rgb_hit = _splash_rgb_cache.get(i)
                    else:
                        bgra_hit = _splash_bgra_cache.get(i)
                if rgb_hit is None and bgra_hit is None:
                    # Keep ``splash_idx`` parked and re-enter shortly; absolute scheduler catches up.
                    root.after(2, splash_tick)
                    return

            splash_idx[0] = i + 1

            try:
                if rgb_hit is not None:
                    # Fast path: opaque RGB → Tk's direct blit (no per-pixel alpha work).
                    splash_photo[0] = ImageTk.PhotoImage(image=Image.fromarray(rgb_hit, "RGB"))
                else:
                    # Fade-tail path: apply the dynamic 1 → 0 alpha ramp to the cached BGRA.
                    fade_mul = splash_end_fade_factor(
                        i, ntot, min(_splash_fade_frames, ntot)
                    )
                    bgra_out = apply_splash_global_alpha(bgra_hit, fade_mul)
                    splash_photo[0] = ImageTk.PhotoImage(image=bgra_to_pil_rgba(bgra_out))
            except Exception:
                # Best-effort RGB fallback so a single bad frame doesn't abort the splash.
                try:
                    fb = rgb_hit if rgb_hit is not None else flatten_bgra_over_bg_to_rgb(
                        bgra_hit if bgra_hit is not None else np.zeros((WINDOW_H, WINDOW_W, 4), np.uint8),
                        _splash_bg_bgr,
                    )
                    splash_photo[0] = ImageTk.PhotoImage(image=Image.fromarray(fb, "RGB"))
                except Exception:
                    pass
            splash_label.configure(image=splash_photo[0])

            # Absolute frame scheduling: target ``splash_t0 + (i+1) * frame_dt`` so slow ticks
            # don't accumulate drift — we drop back to 1 ms if we're already behind.
            now = time.monotonic()
            target = splash_t0[0] + float(i + 1) * frame_dt
            delay_ms = int(round((target - now) * 1000.0))
            if delay_ms < 1:
                delay_ms = 1
            elif delay_ms > frame_ms * 3:
                delay_ms = frame_ms
            root.after(delay_ms, splash_tick)

    else:
        loading = tk.Label(
            content_host,
            text="Starting Pigeon…\n\n"
            "Shift+Tab toggles Settings ↔ off. Key 5 shows the design grid overlay. Tab opens Settings. "
            "Return opens the command bar in Settings, grid overlay (5), or legacy grid mode. "
            "Esc closes the bar or quits. F10 / double-click toggles the display. "
            "Space = play/pause on the selected Player (Apple TV / Roku) when set; else TMDb backdrop "
            "+ logo when loaded; else landing brightness pulse.",
            justify="center",
            fg="#ddd",
            bg="#111",
            wraplength=WINDOW_W - 40,
        )
        loading.pack(expand=True, fill="both")
        startup_ph[0] = loading

    root.update_idletasks()
    root.update()
    if _PIGEON_EXT and startup_ph[0] is not None:
        try:
            startup_ph[0].lift(content_host)
        except tk.TclError:
            try:
                startup_ph[0].lift()
            except tk.TclError:
                pass

    # Keep idle/paused composites intentionally slower to reduce Tk PhotoImage upload pressure.
    paused_interval_ms = max(67, MIC_VIZ_IDLE_COMPOSITE_MS)

    def bootstrap() -> None:
        nonlocal cap

        cap = None

        if not _PIGEON_EXT:
            _w0 = startup_ph[0]
            if _w0 is not None:
                try:
                    _w0.destroy()
                except tk.TclError:
                    pass
                startup_ph[0] = None
        else:
            tk.Widget.pack = _pack_patched  # type: ignore[method-assign]
            tk.Widget.grid = _grid_patched  # type: ignore[method-assign]
            tk.Widget.place = _place_patched  # type: ignore[method-assign]

        try:
            mig = consolidate_legacy_pigeondata_media_folders()
            for line in mig:
                sys.stderr.write(f"pigeon: media folders: {line}\n")
            if mig:
                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"pigeon: media folder consolidation: {e}\n")
            sys.stderr.flush()

        # Full-size video area (always WINDOW_H) so scene scale matches non-overlay mode.
        video_area = tk.Frame(content_host, bg="#000")
        video_area.pack(fill=tk.BOTH, expand=True)

        label = tk.Label(video_area, bd=0, highlightthickness=0, takefocus=True, bg="#000")
        label.pack(fill=tk.BOTH, expand=True)

        settings_frame = tk.Frame(video_area, bg="#111")
        settings_scroll_outer = tk.Frame(settings_frame, bg="#111")
        settings_scroll_outer.pack(fill=tk.BOTH, expand=True)
        settings_canvas = tk.Canvas(
            settings_scroll_outer,
            bg="#111",
            highlightthickness=0,
            bd=0,
        )
        settings_scrollbar = tk.Scrollbar(
            settings_scroll_outer,
            orient=tk.VERTICAL,
            command=settings_canvas.yview,
            bg="#2a2a2e",
            troughcolor="#111",
            activebackground="#3a3a40",
            highlightthickness=0,
        )
        settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        settings_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        settings_inner = tk.Frame(settings_canvas, bg="#111")
        _settings_inner_win = settings_canvas.create_window((16, 12), window=settings_inner, anchor=tk.NW)

        _tk_font_families = frozenset(tkfont.families(root))
        _settings_sans_face = "Helvetica"
        for _cand in (
            "SharpSans",
            "Sharp Sans",
            "SharpSans-Medium",
            "Sharp Sans Medium",
            "SharpSansMedium",
            "Sharp Sans No2",
            "Sharp Sans No1",
        ):
            if _cand in _tk_font_families:
                _settings_sans_face = _cand
                break
        else:
            if "Helvetica Neue" in _tk_font_families:
                _settings_sans_face = "Helvetica Neue"
        _S = _settings_sans_face
        S_FONT_PAGE = (_S, 18, "bold")
        S_FONT_SEC = (_S, 12, "bold")
        S_FONT_BODY = (_S, 10)
        S_FONT_STATUS = (_S, 11)
        S_FONT_BTN = (_S, 11)
        S_FONT_LIST = (_S, 12)
        S_FONT_SMALL = (_S, 9)
        S_FONT_MICRO = (_S, 9)
        S_FONT_CAP_BOLD = (_S, 9, "bold")

        tk.Label(
            settings_inner,
            text="Pigeon Settings",
            fg="#f5f5f5",
            bg="#111",
            font=S_FONT_PAGE,
        ).pack(anchor=tk.W, pady=(0, 4))

        # Inner <Configure> fires often while scrolling an embedded window; only refresh scrollregion
        # when the inner frame actually changes size to avoid canvas flicker / jumpy redraws.
        _settings_inner_scroll_size: list[int] = [0, 0]

        def _settings_update_scrollregion(event: tk.Event | None = None) -> None:
            try:
                if event is not None and getattr(event, "widget", None) is not settings_inner:
                    return
                if event is not None:
                    settings_inner.update_idletasks()
                    rw = int(settings_inner.winfo_reqwidth())
                    rh = int(settings_inner.winfo_reqheight())
                    if rw == _settings_inner_scroll_size[0] and rh == _settings_inner_scroll_size[1]:
                        return
                    _settings_inner_scroll_size[0] = rw
                    _settings_inner_scroll_size[1] = rh
                settings_canvas.update_idletasks()
                bbox = settings_canvas.bbox("all")
                if bbox:
                    settings_canvas.configure(scrollregion=bbox)
            except tk.TclError:
                pass

        def _settings_on_canvas_configure(event: tk.Event) -> None:
            try:
                inner_w = max(1, int(event.width) - 32)
                settings_canvas.itemconfig(_settings_inner_win, width=inner_w)
            except tk.TclError:
                pass
            root.after_idle(lambda: _settings_update_scrollregion(None))

        settings_inner.bind("<Configure>", _settings_update_scrollregion)
        settings_canvas.bind("<Configure>", _settings_on_canvas_configure)

        settings_wheel_all_bound = [False]
        last_pigeon_user_activity_mono = [time.monotonic()]

        def _bump_pigeon_user_activity(_event: object | None = None) -> None:
            """Marks local UI activity so theater idle-dim can ease out (see also bind_all + hotkey handlers)."""
            last_pigeon_user_activity_mono[0] = time.monotonic()

        def _settings_wheel_target_should_ignore(widget: tk.Misc) -> bool:
            """Let Listbox/Text/Entry keep their own scroll behavior."""
            try:
                w: tk.Misc | None = widget
                while w is not None:
                    cls = w.winfo_class()
                    if cls in ("Listbox", "Text", "Entry", "TEntry", "TCombobox"):
                        return True
                    master = w.master
                    w = master if isinstance(master, tk.Misc) else None
            except tk.TclError:
                pass
            return False

        def _settings_is_under_scroll_surface(widget: tk.Misc) -> bool:
            """True when ``widget`` is the settings canvas, scrollbar, or any descendant."""
            try:
                w: tk.Misc | None = widget
                while w is not None:
                    if w is settings_scroll_outer:
                        return True
                    master = w.master
                    w = master if isinstance(master, tk.Misc) else None
            except tk.TclError:
                pass
            return False

        def _settings_mousewheel(event: tk.Event) -> str | None:
            _bump_pigeon_user_activity(event)
            if dev_phase != DevPhase.SETTINGS or not settings_frame.winfo_ismapped():
                return None
            try:
                under = root.winfo_containing(event.x_root, event.y_root)
            except tk.TclError:
                under = None
            if under is None or not _settings_is_under_scroll_surface(under):
                return None
            if _settings_wheel_target_should_ignore(under):
                return None
            try:
                if sys.platform == "darwin":
                    d = int(getattr(event, "delta", 0) or 0)
                    if d == 0:
                        return "break"
                    steps = max(1, abs(d) // 120) if abs(d) >= 120 else 1
                    settings_canvas.yview_scroll(-steps if d > 0 else steps, "units")
                else:
                    num = int(getattr(event, "num", 0) or 0)
                    if num == 4:
                        settings_canvas.yview_scroll(-3, "units")
                    elif num == 5:
                        settings_canvas.yview_scroll(3, "units")
            except tk.TclError:
                pass
            return "break"

        def _settings_bind_wheel_globals() -> None:
            if settings_wheel_all_bound[0]:
                return
            root.bind_all("<MouseWheel>", _settings_mousewheel)
            root.bind_all("<Button-4>", _settings_mousewheel)
            root.bind_all("<Button-5>", _settings_mousewheel)
            settings_wheel_all_bound[0] = True

        def _settings_unbind_wheel_globals() -> None:
            if not settings_wheel_all_bound[0]:
                return
            try:
                root.unbind_all("<MouseWheel>")
                root.unbind_all("<Button-4>")
                root.unbind_all("<Button-5>")
            except tk.TclError:
                pass
            settings_wheel_all_bound[0] = False

        def on_purge_image_media() -> None:
            if not messagebox.askokcancel(
                "Purge image media",
                "Delete all files in pigeonPulledMedia and pigeonReformattedMedia?",
                parent=root,
            ):
                return
            ok1, msg1 = purge_directory_contents(pigeon_pulled_media_dir())
            ok2, msg2 = purge_directory_contents(pigeon_reformatted_media_dir())
            if ok1 and ok2:
                messagebox.showinfo("Purge image media", f"{msg1}\n{msg2}")
            else:
                messagebox.showerror("Purge image media", f"{msg1}\n{msg2}")

        location_section = tk.Frame(settings_inner, bg="#111")
        location_section.pack(anchor=tk.W, fill=tk.X, pady=(8, 2))
        location_menu_var = tk.StringVar(value="")
        location_name_var = tk.StringVar(value="")
        location_option_holder: list[tk.OptionMenu | None] = [None]
        location_om_parent = tk.Frame(location_section, bg="#111")
        location_om_parent.pack(anchor=tk.W, fill=tk.X)
        tk.Label(
            location_om_parent,
            text="Current location",
            fg="#ccc",
            bg="#111",
            font=S_FONT_SEC,
        ).pack(side=tk.LEFT)
        location_om_frame = tk.Frame(location_om_parent, bg="#111")
        location_om_frame.pack(side=tk.LEFT, padx=(10, 0))
        delete_location_btn = tk.Button(
            location_om_parent,
            text="\u2715",
            font=("Helvetica", 12, "bold"),
            fg="#c44",
            bg="#111",
            activebackground="#2a1a1a",
            activeforeground="#f66",
            highlightthickness=1,
            highlightbackground="#553333",
            bd=0,
            cursor="hand2",
            padx=8,
            pady=0,
            command=lambda: None,
            state=tk.DISABLED,
        )
        delete_location_btn.pack(side=tk.LEFT, padx=(10, 0), anchor=tk.W)
        location_edit_row = tk.Frame(location_section, bg="#111")
        location_edit_row.pack(anchor=tk.W, fill=tk.X, pady=(6, 0))
        tk.Label(
            location_edit_row,
            text="Location name",
            fg="#888",
            bg="#111",
            font=S_FONT_MICRO,
        ).pack(side=tk.LEFT)
        location_name_entry = tk.Entry(
            location_edit_row,
            textvariable=location_name_var,
            width=28,
            bg="#1a1a1e",
            fg="#e8e8e8",
            insertbackground="#e8e8e8",
            highlightthickness=1,
            highlightbackground="#333",
            font=S_FONT_BODY,
        )
        location_name_entry.pack(side=tk.LEFT, padx=(8, 6))

        def _apply_location_rename() -> None:
            cid = (read_current_location_id() or "").strip()
            if not cid:
                return
            new_nm = (location_name_var.get() or "").strip() or "Room"
            if not rename_location_v2(cid, new_nm):
                return
            _refresh_location_selector()
            _apply_persisted_location_to_runtime()
            _start_location_toast()

        rename_name_btn = tk.Button(
            location_edit_row,
            text="Save name",
            command=_apply_location_rename,
            font=S_FONT_BTN,
            padx=10,
            pady=2,
        )
        rename_name_btn.pack(side=tk.LEFT)

        def _on_location_name_return(_event: tk.Event) -> str:
            _apply_location_rename()
            return "break"

        location_name_entry.bind("<Return>", _on_location_name_return)
        tk.Label(
            location_section,
            text="Playback and overlay follow the active location. Rename anytime; home room names are not inferred from device scan — set them here or when adding a custom location in Find device.",
            fg="#666",
            bg="#111",
            font=S_FONT_MICRO,
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 0))

        apple_tv_section = tk.Frame(settings_inner, bg="#111")
        apple_tv_section.pack(anchor=tk.W, pady=(4, 3))
        streaming_slot_holder: list[dict[str, str] | None] = [None]
        avr_slot_holder: list[dict[str, str] | None] = [None]
        receiver_poll_busy = {"active": False}
        # Recent pyatv scan (non-empty results only); avoids full LAN scan when adding more devices.
        DISCOVERY_CACHE_TTL_S = 600.0
        discovery_scan_cache: dict[str, object] = {"rows": None, "mono_s": 0.0}
        pairing_led_holder: list[tk.Canvas | None] = [None, None]
        pair_led_busy = {"active": False}
        _pair_led_pending_retry = [False]
        current_apple_tv = read_last_apple_tv()
        receiver_http_host = {"host": str(read_last_receiver().get("host") or "").strip()}
        apple_tv_status_var = tk.StringVar()
        streaming_row_led_canvas_holder: list[tk.Canvas | None] = [None]
        receiver_panel_led_holder: list[tk.Canvas | None] = [None]
        paired_ui_leds: dict[str, tk.Canvas | None] = {"remote": None, "airplay": None, "receiver": None}
        paired_observed_led_rows: list[tuple[tk.Canvas, str, dict[str, str]]] = []
        paired_observed_led_last_state: dict[int, bool | None] = {}
        settings_footer_debug_holder: list[tk.Button | None] = [None]
        settings_footer_reset_holder: list[tk.Button | None] = [None]
        match_quality_glance_label_holder: list[tk.Label | None] = [None]
        match_quality_glance_sig: list[str] = [""]
        apple_tv_busy = {"active": False}
        apple_tv_auto_state: dict[str, object] = {
            "running": False,
            "content_key": None,
            "tmdb_key": None,
            "query": None,
            "prefer": "auto",
            "last_metadata": None,
            # Last TMDb worker actually started (see spawn_tmdb_poster_fetch); for view 4 debug.
            "last_tmdb_fetch_input": None,
            "last_tmdb_fetch_refined": None,
            "last_tmdb_fetch_prefer": None,
        }
        tmdb_retry_rule_idx = [0]
        tmdb_adv_manual_btn_holder: list[tk.Button | None] = [None]
        tmdb_adv_report_btn_holder: list[tk.Button | None] = [None]
        tmdb_adv_log_text_holder: list[tk.Misc | None] = [None]

        def _register_tmdb_adv_widgets(manual: tk.Button, report: tk.Button, log_w: tk.Misc) -> None:
            tmdb_adv_manual_btn_holder[0] = manual
            tmdb_adv_report_btn_holder[0] = report
            tmdb_adv_log_text_holder[0] = log_w

        def _unregister_tmdb_adv_widgets() -> None:
            tmdb_adv_manual_btn_holder[0] = None
            tmdb_adv_report_btn_holder[0] = None
            tmdb_adv_log_text_holder[0] = None

        def _append_tmdb_retry_log_ui(line: str) -> None:
            w = tmdb_adv_log_text_holder[0]
            if w is None:
                return
            try:
                w.insert(tk.END, line + "\n")
                w.see(tk.END)
                body = w.get("1.0", "end-1c")
                if body.count("\n") > 130:
                    w.delete("1.0", "31.0")
            except tk.TclError:
                pass

        apple_tv_playback_clock: dict[str, object] = {
            "has_sync": False,
            "sync_mono": 0.0,
            "sync_position": 0.0,
            "live_mode": False,
            "playing": False,
            "latched_total": None,
            "latched_content_key": None,
            "last_reported_total": None,
            # Steady on-screen TRT: integer shown seconds (slewed), not raw extrapolation.
            "display_played_sec": None,
            "trt_next_fire_mono": None,
        }

        def _metadata_is_netflix_app(metadata: dict[str, object] | None) -> bool:
            if not isinstance(metadata, dict):
                return False
            bid = str(metadata.get("app_id") or "").strip().lower()
            bid2 = str(
                metadata.get("bundle_identifier")
                or metadata.get("app_identifier")
                or metadata.get("bundle_id")
                or ""
            ).strip().lower()
            an = str(metadata.get("app_name") or "").strip().lower()
            return "netflix" in an or "netflix" in bid or "netflix" in bid2

        def _atv_metadata_is_content_idle(metadata: dict[str, object]) -> bool:
            # Netflix often omits title/query; keep UI "active" while the app is foreground.
            if _metadata_is_netflix_app(metadata):
                return False
            ds = str(metadata.get("device_state") or "")
            if "Idle" in ds or "Stopped" in ds:
                return True
            playing_now = "Playing" in ds
            q = str(metadata.get("query") or "").strip()
            return not playing_now and not q

        def _show_paused_row_overlay() -> bool:
            """True when the player has substantive content loaded but is not actively playing."""
            lm_raw = apple_tv_auto_state.get("last_metadata")
            lm = lm_raw if isinstance(lm_raw, dict) else None
            if lm is None:
                return False
            if _atv_metadata_is_content_idle(lm):
                return False
            clk = apple_tv_playback_clock
            if clk.get("live_mode"):
                return False
            ds = str(lm.get("device_state") or "")
            if "Playing" in ds:
                return False
            if "Idle" in ds or "Stopped" in ds:
                return False
            has_title = bool(
                str(lm.get("title") or "").strip()
                or str(lm.get("query") or "").strip()
                or str(lm.get("artist") or "").strip()
            )
            if clk.get("has_sync"):
                return not bool(clk.get("playing"))
            if not has_title:
                return False
            return "Paused" in ds or "Pause" in ds

        apple_tv_dashboard_track: dict[str, object] = {"last_poll_ok": None, "consecutive_fail": 0}
        content_indicator_cv_holder: list[tk.Canvas | None] = [None]
        _LISTBOX_BG = "#1a1a1e"
        _LISTBOX_FG = "#e8e8e8"

        atv_status_row = tk.Frame(apple_tv_section, bg="#111")
        atv_status_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        tk.Label(
            atv_status_row,
            textvariable=apple_tv_status_var,
            fg="#cfcfcf",
            bg="#111",
            font=S_FONT_STATUS,
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        devices_strip = tk.Frame(apple_tv_section, bg="#111")
        devices_strip.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        devices_btn_row = tk.Frame(devices_strip, bg="#111")
        devices_btn_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 4))
        find_device_btn = tk.Button(
            devices_btn_row,
            text="Find device",
            command=lambda: _open_find_device_dialog(),
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        find_device_btn.pack(side=tk.LEFT)
        advanced_matrix_btn = tk.Button(
            devices_btn_row,
            text="Advanced",
            command=lambda: _open_advanced_capability_matrix(),
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        advanced_matrix_btn.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(
            devices_strip,
            text="Choose a device role, pick a device or enter Host/IP, then Confirm to save it to the current location.",
            fg="#666",
            bg="#111",
            font=S_FONT_MICRO,
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        paired_devices_inner = tk.Frame(apple_tv_section, bg="#111")
        paired_devices_inner.pack(anchor=tk.W, fill=tk.X, pady=(0, 8))

        content_section = tk.Frame(apple_tv_section, bg="#111")
        content_section.pack(anchor=tk.W, fill=tk.X, pady=(2, 6))
        content_heading_row = tk.Frame(content_section, bg="#111")
        content_heading_row.pack(anchor=tk.W, fill=tk.X, pady=(0, 6))
        tk.Label(
            content_heading_row,
            text="Content",
            fg="#ccc",
            bg="#111",
            font=S_FONT_SEC,
        ).pack(side=tk.LEFT)
        content_indicator_cv = tk.Canvas(
            content_heading_row,
            width=20,
            height=20,
            bg="#111",
            highlightthickness=0,
            bd=0,
        )
        content_indicator_cv.pack(side=tk.LEFT, padx=(10, 6))
        content_indicator_cv_holder[0] = content_indicator_cv
        _paint_boolean_led(content_indicator_cv, False)
        tk.Label(
            content_heading_row,
            text="● green = detected   ● red = not",
            fg="#666",
            bg="#111",
            font=S_FONT_MICRO,
        ).pack(side=tk.LEFT, padx=(6, 0))
        content_buttons_row = tk.Frame(content_section, bg="#111")
        content_buttons_row.pack(anchor=tk.W, pady=(0, 6))
        # Purge is parented here after handlers. TMDb (Manual Fetch, Report Failure, retry log) lives on Advanced.

        # Bottom “info bar” HUD removed; developer shortcuts are Shift+Tab (cycle) / Tab (settings).
        hud_bar = None
        hud = None

        fps_sched = _default_render_fps()
        display_dims = [WINDOW_W, WINDOW_H]
        fit_holder = [SceneFit(target_w=WINDOW_W, target_h=WINDOW_H)]

        if _PIGEON_EXT:
            from pigeon.design import DESIGN_W as _DESIGN_W_L, DESIGN_H as _DESIGN_H_L

            _land_w, _land_h = int(_DESIGN_W_L), int(_DESIGN_H_L)
        else:
            _land_w, _land_h = WINDOW_W, WINDOW_H
        # No PNG on the landing plate; playback overlay is badge + receiver lines only.
        landing_scene_design_bgr = _build_landing_design_bgr(_land_w, _land_h, None)

        def _disp_fit() -> SceneFit:
            return fit_holder[0]

        def _black_screen_bgr() -> np.ndarray:
            sb, sg, sr = get_stage_bgr()
            out = np.empty((display_dims[1], display_dims[0], 3), dtype=np.uint8)
            out[:] = (sb, sg, sr)
            return out

        frame_interval_ms = max(1, int(round(1000.0 / fps_sched)))
        # Mic visualizer: 24fps target — 60fps + stacked ``after`` timers overwhelmed Tk on some Macs.
        # Mic EQ: higher than video when idle so bars feel tight to the input (Tk load permitting).
        _MIC_VIZ_COMPOSITE_FPS = 48
        _mic_viz_composite_ms = max(1, int(round(1000.0 / _MIC_VIZ_COMPOSITE_FPS)))

        playing = False
        display_view_holder: list[DisplayView] = [DisplayView.ONE]
        # View 4 (key 4): 0=Title Info, 1=Source Info, 2=Playback Info — press 4 again to cycle.
        view_four_subview_holder: list[int] = [0]
        # View 1 (key 1): cycle a -> b -> c layouts (see ``ViewOneLayout``).
        view_one_layout_holder: list[int] = [int(ViewOneLayout.PIGEON_FULL)]

        def _view_one_is_pigeon_full() -> bool:
            return display_view_holder[0] == DisplayView.ONE and int(view_one_layout_holder[0]) == int(
                ViewOneLayout.PIGEON_FULL
            )

        def _view_one_is_pigeon_simple() -> bool:
            return display_view_holder[0] == DisplayView.ONE and int(view_one_layout_holder[0]) == int(
                ViewOneLayout.PIGEON_SIMPLE
            )

        def _view_one_is_pigeon_poster() -> bool:
            return display_view_holder[0] == DisplayView.ONE and int(view_one_layout_holder[0]) == int(
                ViewOneLayout.PIGEON_POSTER
            )
        last_frame: np.ndarray | None = landing_scene_design_bgr
        brightness_current = LANDING_DISPLAY_BRIGHTNESS
        brightness_from = LANDING_DISPLAY_BRIGHTNESS
        brightness_target = LANDING_DISPLAY_BRIGHTNESS
        brightness_t0 = time.monotonic()
        brightness_duration_s = 3.0
        brightness_duration_up_s = 1.0
        brightness_duration_down_s = 1.0

        last_atv_interaction_mono = 0.0
        last_device_interaction_mono = 0.0
        last_timecode_motion_mono = 0.0
        last_clock_saver_significant_device_mono = [time.monotonic()]
        _cs_sig_init = [False]
        _cs_sig_ck: list[str | None] = [None]
        _cs_sig_ds = [""]
        _cs_sig_vol = [""]
        _atv_ix_sig_ds = ""
        _atv_ix_sig_ck: str | None = None
        _atv_ix_pos: float | None = None
        _atv_ix_pos_mono = 0.0
        _atv_ix_extrap_playing = False
        _atv_ix_prev_idle = True

        def _atv_idle_monochrome_active() -> bool:
            """True when theater idle-dim should be fully on (both ATV and Pigeon quiet long enough)."""
            if not current_apple_tv.get("identifier"):
                return False
            # Live TV and some streams never advance ``position``; metadata stays stable for minutes.
            # Without this guard, we never bump ``last_atv_interaction_mono`` and the red idle overlay
            # kicks in after THEATER_IDLE_DIM_AFTER_S even though pyatv still reports Playing.
            if bool(apple_tv_playback_clock.get("playing")):
                return False
            now = time.monotonic()
            pigeon_quiet = (now - last_pigeon_user_activity_mono[0]) >= THEATER_IDLE_DIM_AFTER_S
            if not pigeon_quiet:
                return False
            if last_atv_interaction_mono <= 0.0:
                return True
            return (now - last_atv_interaction_mono) >= THEATER_IDLE_DIM_AFTER_S

        def _vol_norm_for_clock_saver(vol_raw: object) -> str:
            if vol_raw is None:
                return ""
            try:
                v = float(vol_raw)
                if v != v:
                    return ""
                return f"{v:.6g}"
            except (TypeError, ValueError):
                return str(vol_raw).strip()

        def _bump_clock_saver_significant_device() -> None:
            last_clock_saver_significant_device_mono[0] = time.monotonic()

        def _bump_clock_saver_significant_device_from_metadata(md: dict[str, object]) -> None:
            """Selection / play-state / volume changes reset or postpone the clock saver (not timecode drift)."""
            ck = _content_key_from_metadata(md)
            ds = str(md.get("device_state") or "").strip()
            vk = _vol_norm_for_clock_saver(md.get("volume_percent"))
            if not _cs_sig_init[0]:
                _cs_sig_init[0] = True
                _cs_sig_ck[0] = ck
                _cs_sig_ds[0] = ds
                _cs_sig_vol[0] = vk
                return
            bump = False
            if ck != _cs_sig_ck[0] and (ck or _cs_sig_ck[0]):
                bump = True
            if ds != _cs_sig_ds[0]:
                bump = True
            if vk != _cs_sig_vol[0] and (vk or _cs_sig_vol[0]):
                bump = True
            _cs_sig_ck[0] = ck
            _cs_sig_ds[0] = ds
            _cs_sig_vol[0] = vk
            if bump:
                _bump_clock_saver_significant_device()

        def _reset_clock_saver_device_signal_baseline() -> None:
            _cs_sig_init[0] = False
            _cs_sig_ck[0] = None
            _cs_sig_ds[0] = ""
            _cs_sig_vol[0] = ""
            _bump_clock_saver_significant_device()

        def _apply_position_stall_grace_to_clock_saver(now: float) -> None:
            """Keep the saver device-signal timer pinned to ``now`` while content position is live.

            The saver timer is "paused" (continually bumped to ``now``) for as long as the reported
            content position has advanced within :data:`CLOCK_SAVER_POSITION_STALL_GRACE_S`. Once the
            position stays flat beyond that window, we stop bumping and the existing 300 s
            ``CLOCK_SAVER_AFTER_S`` accumulator begins counting from the last advance. A fresh
            position advance during an already-open saver drops the dev-idle span back to zero here,
            so ``_clock_saver_active`` returns ``False`` on the very next tick → saver ends.
            """
            lm = last_timecode_motion_mono
            if lm <= 0.0:
                return
            if (now - lm) >= CLOCK_SAVER_POSITION_STALL_GRACE_S:
                return
            last_clock_saver_significant_device_mono[0] = now

        def _clock_saver_active(now: float) -> bool:
            if clock_saver_composite_bgra is None:
                return False
            if dev_phase != DevPhase.OFF:
                return False
            if not scene_enabled:
                return False
            _apply_position_stall_grace_to_clock_saver(now)
            ui_idle = (now - float(last_pigeon_user_activity_mono[0])) >= CLOCK_SAVER_AFTER_S
            dev_idle = (now - float(last_clock_saver_significant_device_mono[0])) >= CLOCK_SAVER_AFTER_S
            return ui_idle and dev_idle

        def _effective_display_view() -> DisplayView:
            return display_view_holder[0]

        def _clock_saver_layer_opacity(now: float) -> float:
            if now < clock_saver_peek_until_mono[0]:
                return 1.0
            # View 3: dedicated clock layout — saver text stays at full opacity (not idle-dimmed).
            if _effective_display_view() == DisplayView.THREE:
                return 1.0
            return CLOCK_SAVER_DIM_OPACITY

        def _backdrop_active_for_view() -> bool:
            """True when backdrop scene should be used by the current effective view."""
            return bool(use_backdrop_scene and _effective_display_view() != DisplayView.SIX)

        def _design_grid_overlay_active() -> bool:
            """19×8 design grid on the composite (developer GRID phase or view 5)."""
            return dev_phase == DevPhase.GRID or display_view_holder[0] == DisplayView.FIVE

        def _clock_saver_for_compose(now: float) -> bool:
            """True when the large saver time/date patches should be drawn (idle on most views; always on view 3)."""
            if clock_saver_composite_bgra is None:
                return False
            if dev_phase != DevPhase.OFF:
                return False
            if not scene_enabled:
                return False
            ev = _effective_display_view()
            if ev == DisplayView.FOUR:
                return False
            if ev == DisplayView.THREE:
                return True
            return _clock_saver_active(now)

        def _app_logo_clock_saver_style_now() -> bool:
            """Dim, row-2–top app logo layout when there is no TMDb still (letterbox master) in saver contexts."""
            if not backdrop_app_logo_letterbox_fit:
                return False
            return _effective_display_view() == DisplayView.THREE or _clock_saver_for_compose(
                time.monotonic()
            )

        def _backdrop_bgr_for_view_two() -> np.ndarray | None:
            if use_backdrop_scene and backdrop_master_bgr is not None:
                return backdrop_master_bgr
            if saved_backdrop_master_bgr is not None:
                return saved_backdrop_master_bgr
            return None

        def _clock_saver_backdrop_brightness(now: float) -> float:
            """1 = full brightness; idle clock-saver on backdrop uses ``CLOCK_SAVER_BACKDROP_DIM``."""
            if _effective_display_view() == DisplayView.THREE:
                return 1.0
            if not _backdrop_active_for_view() or backdrop_master_bgr is None:
                return 1.0
            if not _clock_saver_for_compose(now):
                return 1.0
            return float(CLOCK_SAVER_BACKDROP_DIM)

        def _clock_saver_dim_pre_digit_canvas(canvas: np.ndarray, dim: float) -> None:
            """Darken backdrop/gradient/mic before drawing saver glyphs so only the large time reads at full brilliance."""
            d = max(0.0, min(1.0, float(dim)))
            if d >= 1.0 - 1e-9:
                return
            canvas[:] = (canvas.astype(np.float32) * d).clip(0, 255).astype(np.uint8)

        def _clock_saver_dim_overlay_bgra(patch_bgra: np.ndarray, dim: float) -> np.ndarray:
            """Idle-dim streaming badge / receiver rows during clock saver (alpha scale; callers may skip if dim≈1)."""
            d = max(0.0, min(1.0, float(dim)))
            if d >= 1.0 - 1e-9:
                return patch_bgra
            out = patch_bgra.copy().astype(np.float32)
            out[:, :, 3] *= d
            return np.clip(out, 0, 255).astype(np.uint8)

        idle_dim_anim_strength = 0.0
        _idle_dim_anim_goal = 0.0
        _idle_dim_anim_from = 0.0
        _idle_dim_anim_t0 = time.monotonic()

        def _update_idle_dim_strength(now: float) -> float:
            """0 = full color, 1 = red luma-mono; eases in/out over ATV_IDLE_MONO_ANIM_S when combined idle state changes."""
            nonlocal idle_dim_anim_strength, _idle_dim_anim_goal, _idle_dim_anim_from, _idle_dim_anim_t0
            if not THEATER_IDLE_DIM_ENABLED:
                if idle_dim_anim_strength != 0.0 or _idle_dim_anim_goal != 0.0:
                    idle_dim_anim_strength = 0.0
                    _idle_dim_anim_goal = 0.0
                    _idle_dim_anim_from = 0.0
                    _idle_dim_anim_t0 = now
                return 0.0
            want = 1.0 if _atv_idle_monochrome_active() else 0.0
            if want != _idle_dim_anim_goal:
                _idle_dim_anim_goal = want
                _idle_dim_anim_from = idle_dim_anim_strength
                _idle_dim_anim_t0 = now
            dur = float(ATV_IDLE_MONO_ANIM_S)
            t = min(1.0, (now - _idle_dim_anim_t0) / dur) if dur > 0 else 1.0
            idle_dim_anim_strength = _idle_dim_anim_from + (_idle_dim_anim_goal - _idle_dim_anim_from) * t
            return idle_dim_anim_strength

        _compose_idle_strength_holder: list[float] = [0.0]

        scaled_display: np.ndarray | None = None
        scaled_version = 0
        if last_frame is not None:
            scaled_display = _disp_fit().scale_and_crop(last_frame)
            scaled_version = 1

        skip_cache: tuple[object, ...] | None = None
        scene_enabled = _load_persisted_scene_enabled(True)
        dev_phase = DevPhase.OFF
        # Advanced matrix: temporarily show GRID behind the dialog when opened from Settings; restore on close.
        advanced_matrix_restore_phase: list[object] = [None]
        advanced_matrix_close_skip: list[bool] = [False]
        location_toast_state: dict[str, object] = {
            "active": False,
            "text": "",
            "t0": 0.0,
            "startup_top_left": False,
        }
        prev_dev_phase_for_location_toast: list[DevPhase] = [DevPhase.OFF]
        clock_saver_peek_until_mono: list[float] = [0.0]
        black_photo: ImageTk.PhotoImage | None = None
        label_live_photo: list[ImageTk.PhotoImage | None] = [None]
        _render_after_id: list[str | None] = [None]
        use_backdrop_scene = False
        backdrop_master_bgr: np.ndarray | None = None
        # Last TMDb backdrop (copy); survives display off so developer-grid F10 can return to backdrop.
        saved_backdrop_master_bgr: np.ndarray | None = None
        # True when the saved/current master came from the streaming app logo (not TMDb stills).
        saved_backdrop_app_logo_letterbox_fit: bool = False
        backdrop_app_logo_letterbox_fit: bool = False

        if _PIGEON_EXT and prepare_default_poster_at_startup is not None:
            try:
                ok_sp, msg_sp, _gc_sp = prepare_default_poster_at_startup()
                sys.stderr.write(f"pigeon: startup poster: {msg_sp}\n")
                sys.stderr.flush()
            except Exception as e:
                sys.stderr.write(f"pigeon: startup poster error: {e}\n")
                sys.stderr.flush()

        clock_widget = (
            ClockCalendarWidget(
                anchor_row=CLOCK_WIDGET_ROW,
                anchor_col_right=float(VIEW_ONE_CLOCK_COL_RIGHT),
            )
            if _PIGEON_EXT and ClockCalendarWidget is not None
            else None
        )
        tmdb_logo_widget = (
            TmdbLogoWidget(
                anchor_row=TMDB_LOGO_ANCHOR_ROW,
                anchor_col=0,
                span_wide=TMDB_LOGO_SPAN_W,
                span_tall=TMDB_LOGO_SPAN_H,
                fit_scale=TMDB_LOGO_FIT_SCALE,
                vertical_align="top",
                top_right_col_1based=float(TMDB_LOGO_TOP_RIGHT_COL),
            )
            if _PIGEON_EXT and TmdbLogoWidget is not None
            else None
        )
        tmdb_logo_widget_view_six = (
            TmdbLogoWidget(
                anchor_row=TMDB_LOGO_VIEW6_ANCHOR_ROW,
                anchor_col=TMDB_LOGO_VIEW6_ANCHOR_COL,
                span_wide=TMDB_LOGO_VIEW6_SPAN_W,
                span_tall=TMDB_LOGO_VIEW6_SPAN_H,
                fit_scale=TMDB_LOGO_VIEW6_FIT_SCALE,
                vertical_align="center",
            )
            if _PIGEON_EXT and TmdbLogoWidget is not None
            else None
        )
        active_tmdb_title_key: str | None = None
        active_tmdb_display_title: str | None = None
        # When TMDb returns no match for the current playback title, surface the streaming
        # app's own logo in the content-logo slot instead of popping an error dialog.
        # ``_warm_tmdb_logo_patch`` consults this flag whenever no TMDb logo is active.
        tmdb_logo_app_fallback_active: bool = False
        # Contrast-aware bottom gradient tint: ``_warm_tmdb_logo_patch`` evaluates the active
        # TT logo via ``pigeon.tmdb_tt_contrast.pick_gradient_bgr`` and stores the winning
        # ``(B, G, R)`` here. Default black preserves legacy behaviour when no TT is loaded.
        tmdb_tt_gradient_bgr_holder: list[tuple[int, int, int]] = [GRADIENT_BGR_DARK]
        status_bar_widget = None
        if _PIGEON_EXT and StatusBarWidget is not None:
            status_bar_widget = StatusBarWidget(
                assets_dir=Path(_PROJECT_DIR) / "pigeonAssets",
                trt_row=TRT_DISPLAY_ROW,
                trt_played_col=TRT_PLAYED_COL,
                trt_remaining_col=TRT_REMAINING_COL,
                trt_played_text=TRT_PLAYED_TEXT,
                trt_remaining_text=TRT_REMAINING_TEXT,
                trt_label_span_wide=TRT_LABEL_SPAN_W,
                trt_label_span_tall=TRT_LABEL_SPAN_H,
            )

        receiver_overlay_state: dict[str, str] = {
            "incoming": "",
            "config": "",
            "volume": "",
        }
        receiver_telnet_debug_holder: list[dict[str, str]] = [{}]
        # Track the last usable Denon volume reading so the Apple TV metadata poll (which
        # reports ``volume_percent=0`` when an AV receiver owns the volume line) does not
        # briefly overwrite the authoritative dB value on its own cadence. The receiver
        # poll keeps running on its own schedule; this cache only controls *display*.
        denon_vol_cache: dict[str, object] = {
            "effective": "",
            "mono_usable": 0.0,
        }
        streaming_badge_state: dict[str, object] = {
            "show": False,
            "filename": "",
            "label": "",
        }
        playback_overlay_flags: dict[str, bool] = {
            "show_paused_row": False,
            "clock_saver_volume_only": False,
            "clock_saver_netflix_full_overlay": False,
        }
        playback_overlay_widget = None
        if _PIGEON_EXT and PlaybackOverlayWidget is not None:
            playback_overlay_widget = PlaybackOverlayWidget(
                assets_dir=Path(_PROJECT_DIR) / "pigeonAssets",
                receiver_state=receiver_overlay_state,
                service_badge=streaming_badge_state,
                overlay_flags=playback_overlay_flags,
                badge_top_right_col_1based=VIEW_ONE_BADGE_COL_RIGHT,
                volume_top_right_col_1based=float(VIEW_ONE_CLOCK_COL_RIGHT),
            )

        def _playback_is_netflix_stream() -> bool:
            lm = apple_tv_auto_state.get("last_metadata")
            if _metadata_is_netflix_app(lm if isinstance(lm, dict) else None):
                return True
            sb = streaming_badge_state
            lbl = str(sb.get("label") or "").strip().lower()
            fn = str(sb.get("filename") or "").strip().lower()
            return "netflix" in lbl or "netflix" in fn

        def _backdrop_master_from_streaming_app_logo() -> np.ndarray | None:
            """Letterbox streaming app logo on black; resolves path from metadata when badge file unset."""
            assets_root = Path(_PROJECT_DIR) / "pigeonAssets"
            fn = str(streaming_badge_state.get("filename") or "").strip()
            if not fn:
                lm = apple_tv_auto_state.get("last_metadata")
                if isinstance(lm, dict) and _metadata_is_netflix_app(lm):
                    from pigeon.streaming_service_badges import resolve_streaming_badge_media

                    fn2, _lbl = resolve_streaming_badge_media(
                        assets_root,
                        app_name=str(lm.get("app_name") or ""),
                        app_id=str(lm.get("app_id") or ""),
                    )
                    fn = (fn2 or "").strip()
            if not fn:
                return None
            p = assets_root / fn
            if not p.is_file():
                return None
            try:
                from pigeon.image_ui_protocol import (
                    app_logo_fallback_master_bgr,
                    bgra_to_bgr_on_black,
                    load_image_bgra,
                )

                bgra = load_image_bgra(p)
                if bgra is None or bgra.size == 0:
                    return None
                bgr = bgra_to_bgr_on_black(bgra)
                return app_logo_fallback_master_bgr(
                    bgr,
                    display_w=int(display_dims[0]),
                    display_h=int(display_dims[1]),
                    fraction=float(APP_LOGO_FALLBACK_MAX_RESOLUTION_FRACTION),
                )
            except Exception:
                return None

        def _apply_netflix_backdrop_when_running() -> bool:
            """Netflix foreground: letterbox Netflix logo as backdrop (swap if scene exists, else open scene)."""
            if not _playback_is_netflix_stream():
                return False
            logo_bd = _backdrop_master_from_streaming_app_logo()
            if logo_bd is None:
                return False
            nonlocal cap, scene_enabled, playing, use_backdrop_scene, backdrop_master_bgr
            nonlocal saved_backdrop_master_bgr, backdrop_app_logo_letterbox_fit, saved_backdrop_app_logo_letterbox_fit
            nonlocal last_frame, scaled_display, scaled_version, skip_cache
            nonlocal active_tmdb_title_key, active_tmdb_display_title, tmdb_logo_patch_bgra
            nonlocal tmdb_logo_app_fallback_active
            nonlocal brightness_current, brightness_from, brightness_target, brightness_t0

            fn_sb = str(streaming_badge_state.get("filename") or "").lower()
            if use_backdrop_scene and backdrop_master_bgr is not None:
                if backdrop_app_logo_letterbox_fit and "netflix" in fn_sb:
                    return False
                backdrop_master_bgr = logo_bd
                saved_backdrop_master_bgr = np.asarray(logo_bd, dtype=np.uint8).copy()
                saved_backdrop_app_logo_letterbox_fit = True
                backdrop_app_logo_letterbox_fit = True
                scaled_display = None
                scaled_version += 1
                skip_cache = None
                if status_bar_widget is not None:
                    bd_arr = np.asarray(logo_bd, dtype=np.uint8)
                    if status_bar_widget.set_accent_from_backdrop_bgr(bd_arr):
                        _warm_status_bar_blits()
                        skip_cache = None
                return True

            if not scene_enabled:
                return False
            if not str(current_apple_tv.get("identifier") or "").strip():
                return False

            active_tmdb_title_key = None
            active_tmdb_display_title = None
            tmdb_logo_app_fallback_active = False
            if tmdb_logo_widget is not None:
                tmdb_logo_widget.clear_cache()
            if tmdb_logo_widget_view_six is not None:
                tmdb_logo_widget_view_six.clear_cache()
            _warm_tmdb_logo_patch()
            tmdb_logo_patch_bgra = None
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                cap = None
            backdrop_master_bgr = logo_bd
            saved_backdrop_master_bgr = np.asarray(logo_bd, dtype=np.uint8).copy()
            saved_backdrop_app_logo_letterbox_fit = True
            backdrop_app_logo_letterbox_fit = True
            use_backdrop_scene = True
            scene_enabled = True
            playing = False
            last_frame = None
            scaled_display = None
            scaled_version += 1
            _save_persisted_scene_enabled(True)
            brightness_current = brightness_from = brightness_target = BACKDROP_BRIGHTNESS
            brightness_t0 = time.monotonic()
            skip_cache = None
            if status_bar_widget is not None:
                bd_arr = np.asarray(logo_bd, dtype=np.uint8)
                if status_bar_widget.set_accent_from_backdrop_bgr(bd_arr):
                    _warm_status_bar_blits()
                    skip_cache = None
            return True

        _pigeon_ui_started_mono = time.monotonic()
        _startup_splash_complete: list[bool] = [False]

        clock_patch_bgra: np.ndarray | None = None
        tmdb_logo_patch_bgra: np.ndarray | None = None
        status_bar_blits: list = []
        playback_overlay_blits: list = []
        # [unix_sec, status_bar accent BGR or None] — clock patch invalidation.
        _clock_patch_sig: list = [-1, None]
        # Optional full-canvas BGRA from ``pigeonAssets/TopGradient.png`` (above backdrop, under widgets).
        top_gradient_design_bgra: list[np.ndarray | None] = [None]
        _top_gradient_load_attempted: list[bool] = [False]

        def _ensure_top_gradient_loaded() -> np.ndarray | None:
            if top_gradient_design_bgra[0] is not None:
                return top_gradient_design_bgra[0]
            if _top_gradient_load_attempted[0]:
                return None
            _top_gradient_load_attempted[0] = True
            p = Path(_PROJECT_DIR) / "pigeonAssets" / "TopGradient.png"
            if not p.is_file():
                return None
            try:
                from pigeon.image_ui_protocol import load_image_bgra

                raw = load_image_bgra(str(p))
                if raw is None or raw.size == 0:
                    return None
                h0, w0 = raw.shape[:2]
                if w0 != DESIGN_W or h0 != DESIGN_H:
                    raw = cv2.resize(
                        raw,
                        (DESIGN_W, DESIGN_H),
                        interpolation=cv_resize_interp(w0, h0, DESIGN_W, DESIGN_H),
                    )
                top_gradient_design_bgra[0] = raw
                return raw
            except Exception:
                return None

        def _blend_top_gradient_design(canvas: np.ndarray) -> None:
            if alpha_blend_bgra_over_bgr is None:
                return
            tg = _ensure_top_gradient_loaded()
            if tg is None:
                return
            ch, cw = int(canvas.shape[0]), int(canvas.shape[1])
            if cw != DESIGN_W or ch != DESIGN_H:
                return
            canvas[:] = alpha_blend_bgra_over_bgr(canvas, tg)

        def _blend_top_gradient_fast(base: np.ndarray, cap_w: int, cap_h: int) -> None:
            if alpha_blend_bgra_over_bgr is None:
                return
            tg = _ensure_top_gradient_loaded()
            if tg is None:
                return
            x, y, rw, rh = _design_rect_to_target(0, 0, DESIGN_W, DESIGN_H, cap_w, cap_h)
            _gh, _gw = tg.shape[:2]
            patch = cv2.resize(tg, (rw, rh), interpolation=cv_resize_interp(_gw, _gh, rw, rh))
            sub = base[y : y + rh, x : x + rw]
            sub[:] = alpha_blend_bgra_over_bgr(sub, patch)

        def _apply_stage_chrome_colors() -> None:
            b, g, r = get_stage_bgr()
            hx = bgr_to_tk_hex(b, g, r)
            try:
                video_area.configure(bg=hx)
                label.configure(bg=hx)
            except tk.TclError:
                pass

        def _refresh_stage_from_poster() -> None:
            nonlocal black_photo, skip_cache
            if not _PIGEON_EXT:
                set_stage_bgr(0, 0, 0)
            else:
                from pigeon.widgets.poster_art import sync_stage_background_from_active_poster

                sync_stage_background_from_active_poster()
            _apply_stage_chrome_colors()
            black_photo = None
            skip_cache = None

        _refresh_stage_from_poster()

        def _design_rect_to_target(
            wx: int, wy: int, ww: int, wh: int, Wt: int, Ht: int
        ) -> tuple[int, int, int, int]:
            """Map a design-canvas rectangle to target size Wt×Ht (same math as final window mapping)."""
            Wd, Hd = DESIGN_W, DESIGN_H
            scaled_w = int(round(Wd * Ht / float(Hd)))
            x_off = max(0, (scaled_w - Wt) // 2)

            def mx(xd: float) -> int:
                return int(round(xd * scaled_w / float(Wd))) - x_off

            def my(yd: float) -> int:
                return int(round(yd * Ht / float(Hd)))

            x0, y0 = mx(wx), my(wy)
            x1, y1 = mx(wx + ww), my(wy + wh)
            rw = max(1, x1 - x0)
            rh = max(1, y1 - y0)
            if x0 < 0:
                rw += x0
                x0 = 0
            if y0 < 0:
                rh += y0
                y0 = 0
            rw = min(rw, Wt - x0)
            rh = min(rh, Ht - y0)
            if rw < 1 or rh < 1:
                return (0, 0, 1, 1)
            return (x0, y0, rw, rh)

        def _design_rect_to_window(wx: int, wy: int, ww: int, wh: int) -> tuple[int, int, int, int]:
            return _design_rect_to_target(wx, wy, ww, wh, display_dims[0], display_dims[1])

        def _warm_status_bar_blits() -> None:
            nonlocal status_bar_blits
            if status_bar_widget is None:
                status_bar_blits = []
                return
            status_bar_blits = list(status_bar_widget.design_blits())

        def _set_playback_overlay_clock_saver_volume_flag() -> None:
            """True while clock saver is active and the receiver shows a real volume string."""
            from pigeon.widgets.playback_overlay import _receiver_volume_display_line

            vol = _receiver_volume_display_line(receiver_overlay_state.get("volume", ""))
            cs_ok = _clock_saver_for_compose(time.monotonic())
            nf_bd = bool(cs_ok and _backdrop_active_for_view() and _playback_is_netflix_stream())
            playback_overlay_flags["clock_saver_netflix_full_overlay"] = nf_bd
            playback_overlay_flags["clock_saver_volume_only"] = bool(
                cs_ok and vol and not nf_bd
            )

        def _warm_playback_overlay_blits() -> None:
            nonlocal playback_overlay_blits
            if playback_overlay_widget is None:
                playback_overlay_blits = []
                return
            playback_overlay_flags["show_paused_row"] = _show_paused_row_overlay()
            _set_playback_overlay_clock_saver_volume_flag()
            playback_overlay_blits = list(playback_overlay_widget.design_blits())

        def _active_tmdb_logo_widget():
            if _effective_display_view() == DisplayView.SIX and tmdb_logo_widget_view_six is not None:
                return tmdb_logo_widget_view_six
            return tmdb_logo_widget

        _tmdb_poster_cache: dict[str, object] = {"key": None, "bgra": None}

        def _styled_video_content_c_poster(src_bgra: np.ndarray | None) -> np.ndarray | None:
            """Return poster BGRA with rounded corners + faint white border for viewOne.videoContent_c."""
            if src_bgra is None or src_bgra.size == 0 or src_bgra.ndim != 3:
                return None
            if src_bgra.shape[2] == 4:
                out = src_bgra.copy()
            elif src_bgra.shape[2] == 3:
                out = cv2.cvtColor(src_bgra, cv2.COLOR_BGR2BGRA)
            else:
                return None
            h, w = int(out.shape[0]), int(out.shape[1])
            if h < 4 or w < 4:
                return out

            radius = max(6, int(round(min(w, h) * 0.035)))
            radius = min(radius, max(1, (min(w, h) // 2) - 1))

            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.rectangle(mask, (radius, 0), (w - radius, h), 255, thickness=-1)
            cv2.rectangle(mask, (0, radius), (w, h - radius), 255, thickness=-1)
            cv2.circle(mask, (radius, radius), radius, 255, thickness=-1)
            cv2.circle(mask, (w - radius, radius), radius, 255, thickness=-1)
            cv2.circle(mask, (radius, h - radius), radius, 255, thickness=-1)
            cv2.circle(mask, (w - radius, h - radius), radius, 255, thickness=-1)

            out_alpha = out[:, :, 3].astype(np.float32) * (mask.astype(np.float32) / 255.0)
            out[:, :, 3] = np.clip(out_alpha, 0, 255).astype(np.uint8)

            stroke = max(1, int(round(min(w, h) * 0.006)))
            if stroke > 0 and (h - 2 * stroke) > 2 and (w - 2 * stroke) > 2:
                inner = np.zeros((h, w), dtype=np.uint8)
                ir = max(1, radius - stroke)
                cv2.rectangle(inner, (stroke + ir, stroke), (w - stroke - ir, h - stroke), 255, thickness=-1)
                cv2.rectangle(inner, (stroke, stroke + ir), (w - stroke, h - stroke - ir), 255, thickness=-1)
                cv2.circle(inner, (stroke + ir, stroke + ir), ir, 255, thickness=-1)
                cv2.circle(inner, (w - stroke - ir, stroke + ir), ir, 255, thickness=-1)
                cv2.circle(inner, (stroke + ir, h - stroke - ir), ir, 255, thickness=-1)
                cv2.circle(inner, (w - stroke - ir, h - stroke - ir), ir, 255, thickness=-1)
                border = cv2.subtract(mask, inner)
            else:
                border = np.zeros((h, w), dtype=np.uint8)
            if np.any(border > 0):
                out[border > 0, 0] = 255
                out[border > 0, 1] = 255
                out[border > 0, 2] = 255
                out[border > 0, 3] = np.maximum(out[border > 0, 3], 96)
            return out

        def _active_tmdb_poster_bgra() -> np.ndarray | None:
            """Return cached TMDb poster BGRA for the active title key (or ``None``)."""
            if not active_tmdb_title_key:
                return None
            try:
                from pigeon.media_cache import ASSET_POSTER_ART, find_cached_reformatted_asset
                from pigeon.image_ui_protocol import load_image_bgra
            except Exception:
                return None
            poster_path = find_cached_reformatted_asset(
                str(active_tmdb_title_key), ASSET_POSTER_ART
            )
            if poster_path is None or not poster_path.is_file():
                return None
            try:
                mtime = poster_path.stat().st_mtime
            except OSError:
                return None
            key = (str(poster_path), float(mtime))
            if _tmdb_poster_cache.get("key") == key:
                hit = _tmdb_poster_cache.get("bgra")
                return hit if isinstance(hit, np.ndarray) else None
            raw = load_image_bgra(poster_path)
            if raw is None or raw.size == 0:
                _tmdb_poster_cache["key"] = key
                _tmdb_poster_cache["bgra"] = None
                return None
            _tmdb_poster_cache["key"] = key
            _tmdb_poster_cache["bgra"] = raw
            return raw

        def _resolve_streaming_app_logo_bgra() -> np.ndarray | None:
            """Resolve the streaming-service badge source BGRA (same filename resolution as the
            app-logo backdrop fallback). Returns ``None`` when no usable image is available."""
            assets_root = Path(_PROJECT_DIR) / "pigeonAssets"
            fn = str(streaming_badge_state.get("filename") or "").strip()
            if not fn:
                lm = apple_tv_auto_state.get("last_metadata")
                if isinstance(lm, dict):
                    try:
                        from pigeon.streaming_service_badges import (
                            resolve_streaming_badge_media,
                        )

                        fn2, _lbl = resolve_streaming_badge_media(
                            assets_root,
                            app_name=str(lm.get("app_name") or ""),
                            app_id=str(lm.get("app_id") or ""),
                        )
                        fn = (fn2 or "").strip()
                    except Exception:
                        fn = ""
            if not fn:
                return None
            p = assets_root / fn
            if not p.is_file():
                return None
            try:
                from pigeon.image_ui_protocol import load_image_bgra

                bgra = load_image_bgra(p)
            except Exception:
                return None
            if bgra is None or bgra.size == 0:
                return None
            return bgra

        def _refresh_tmdb_tt_gradient_tint() -> None:
            """Evaluate TT brightness and pick the bottom-gradient tint (black vs white).

            Runs every time ``_warm_tmdb_logo_patch`` refreshes the cached TT patch. Falls
            back to the legacy dark gradient when no TT is available.
            """
            prev = tmdb_tt_gradient_bgr_holder[0]
            chosen, lum = pick_gradient_bgr(tmdb_logo_patch_bgra)
            tmdb_tt_gradient_bgr_holder[0] = chosen
            if chosen != prev:
                label = "white" if chosen == (255, 255, 255) else "black"
                title = active_tmdb_display_title or active_tmdb_title_key or "(no-title)"
                lum_s = f"{lum:.3f}" if lum is not None else "n/a"
                print(
                    f"pigeon: TT contrast → {label} gradient (luminance={lum_s}, title={title!r})",
                    file=sys.stderr,
                )

        def _warm_tmdb_logo_patch() -> None:
            nonlocal tmdb_logo_patch_bgra
            logo_w = _active_tmdb_logo_widget()
            if logo_w is None:
                tmdb_logo_patch_bgra = None
                _refresh_tmdb_tt_gradient_tint()
                return
            if active_tmdb_title_key:
                tmdb_logo_patch_bgra = logo_w.bgra_patch_for_title(
                    active_tmdb_title_key,
                    display_title=active_tmdb_display_title,
                ).copy()
                _refresh_tmdb_tt_gradient_tint()
                return
            if tmdb_logo_app_fallback_active:
                src = _resolve_streaming_app_logo_bgra()
                if src is not None:
                    tmdb_logo_patch_bgra = logo_w.bgra_patch_from_source_bgra(src).copy()
                    _refresh_tmdb_tt_gradient_tint()
                    return
            tmdb_logo_patch_bgra = None
            _refresh_tmdb_tt_gradient_tint()

        # ---- View 1 fallback-variant detection (viewOne.01 .. .09) --------
        # These probe live state so ``_current_view_one_variant`` can route the
        # View-1 composition path through the correct fallback. See
        # ``pigeon/view_one_variants.py`` for the decision table.
        def _vv_has_content_title() -> bool:
            return bool((active_tmdb_display_title or "").strip())

        def _vv_has_current_app() -> bool:
            if str(streaming_badge_state.get("filename") or "").strip():
                return True
            lm = apple_tv_auto_state.get("last_metadata")
            if isinstance(lm, dict):
                if str(lm.get("app_name") or "").strip():
                    return True
                if str(lm.get("app_id") or "").strip():
                    return True
            return False

        def _vv_has_tmdb_bd() -> bool:
            # A real TMDb backdrop — NOT the app-logo letterbox fallback that
            # reuses ``backdrop_master_bgr`` as a black-canvas app-logo strip.
            if backdrop_master_bgr is not None and not backdrop_app_logo_letterbox_fit:
                return True
            if (
                saved_backdrop_master_bgr is not None
                and not saved_backdrop_app_logo_letterbox_fit
            ):
                return True
            return False

        def _vv_has_tmdb_tt() -> bool:
            return bool(active_tmdb_title_key)

        def _vv_has_app_logo() -> bool:
            return _resolve_streaming_app_logo_bgra() is not None

        def _vv_is_music() -> bool:
            """True when the currently playing media is of type Music.

            Reads ``last_metadata['media_type']`` (populated via pyatv) and accepts
            both the pyatv ``MediaType.Music`` stringified form and the bare ``Music``
            label to be robust across metadata sources.
            """
            lm = apple_tv_auto_state.get("last_metadata")
            if not isinstance(lm, dict):
                return False
            mt = str(lm.get("media_type") or "").strip().lower()
            if not mt:
                return False
            return mt == "music" or mt.endswith(".music")

        def _vv_music_track_title() -> str:
            """Return the preferred Music track title for text rendering.

            Prefers ``title``; falls back to ``album`` (often the only populated
            field for certain streaming sources). Returns an empty string when
            nothing usable is available.
            """
            lm = apple_tv_auto_state.get("last_metadata")
            if not isinstance(lm, dict):
                return ""
            for k in ("title", "album"):
                v = str(lm.get(k) or "").strip()
                if v:
                    return v
            return ""

        def _vv_music_text_lines() -> tuple[str, str]:
            """Return ``(title, subtitle)`` for Music text rendering.

            ``title`` is the track title (large top line); ``subtitle`` is the
            composed ``"Artist - Album"`` string (smaller line beneath) with a
            graceful collapse when either field is missing:

            * both present  → ``"Artist - Album"``
            * artist only   → ``"Artist"``
            * album only    → ``"Album"``
            * neither       → ``""``

            If ``title`` is empty but ``album`` is populated, ``album`` is
            promoted to ``title`` so the large line is never blank; the
            subtitle then collapses to just the artist (if any).
            """
            lm = apple_tv_auto_state.get("last_metadata")
            if not isinstance(lm, dict):
                return ("", "")
            title = str(lm.get("title") or "").strip()
            artist = str(lm.get("artist") or "").strip()
            album = str(lm.get("album") or "").strip()
            if not title and album:
                title, album = album, ""
            if artist and album:
                subtitle = f"{artist} - {album}"
            elif artist:
                subtitle = artist
            elif album:
                subtitle = album
            else:
                subtitle = ""
            return (title, subtitle)

        def _current_view_one_variant():
            if resolve_view_one_variant is None:
                return None
            return resolve_view_one_variant(
                layout_is_simple=(
                    int(view_one_layout_holder[0]) == int(ViewOneLayout.PIGEON_SIMPLE)
                ),
                has_title_meta=_vv_has_content_title(),
                has_app_meta=_vv_has_current_app(),
                has_tmdb_bd=_vv_has_tmdb_bd(),
                has_tmdb_tt=_vv_has_tmdb_tt(),
                has_app_logo=_vv_has_app_logo(),
            )

        def _view_one_variant_uses_full_path() -> bool:
            if _view_one_is_pigeon_poster():
                return False
            v = _current_view_one_variant()
            if v is None or variant_uses_full_path is None:
                return _view_one_is_pigeon_full()
            return variant_uses_full_path(v)

        def _view_one_variant_uses_simple_path() -> bool:
            if _view_one_is_pigeon_poster():
                return False
            v = _current_view_one_variant()
            if v is None or variant_uses_full_path is None:
                return _view_one_is_pigeon_simple()
            return display_view_holder[0] == DisplayView.ONE and not variant_uses_full_path(v)

        def _view_one_video_content_a_tt_contain_rect_design() -> tuple[int, int, int, int]:
            """Design-pixel (x, y, w, h) for pigeonTMDB_TT uniform contain-fit on viewOne.videoContent_a.

            Horizontally the slot is **10 design cells wide**, centered on column **7.5**
            (≈ columns 2.5–12.5): uniform-contain–fit, as large as that band and vertical clearance
            allow. Vertically it clears the streaming badge, receiver-driven overlay lines, and the
            gradient / status region (with a slightly lower floor and tighter gap to the gradient).
            """
            if (
                get_grid_geometry is None
                or rect_for_span_top_right_at_cell is None
                or rect_for_span_at_cell is None
            ):
                return (0, 0, max(1, int(DESIGN_W)), max(1, int(DESIGN_H)))
            g = get_grid_geometry()
            pad = max(4, int(round(0.12 * float(g.cell))))

            bx, by, bw, bh = rect_for_span_top_right_at_cell(
                2,
                1,
                row_1based=0.5,
                col_right_1based=float(VIEW_ONE_BADGE_COL_RIGHT),
            )

            top_min = int(by) + int(bh) + pad
            top_min = max(
                top_min,
                int(round(float(g.y0) + (3.0 - 1.0) * float(g.cell))),
            )
            if playback_overlay_widget is not None:
                try:
                    for _p in playback_overlay_widget.design_blits():
                        if getattr(_p, "layer", "") != PATCH_LAYER_RECEIVER_AUDIO:
                            continue
                        py1 = int(_p.y) + int(_p.h) + pad
                        if py1 > top_min:
                            top_min = min(py1, int(DESIGN_H) - 8)
                except Exception:
                    pass

            # Allow the title treatment to use more vertical band (still below TRT / status row).
            bottom_max = int(round(float(g.y0) + (7.45 - 1.0) * float(g.cell)))
            try:
                if playback_lower_gradient_bgra is not None:
                    _gx, gy, _gw, _gh, _grad = playback_lower_gradient_bgra(
                        gradient_bgr=tmdb_tt_gradient_bgr_holder[0]
                    )
                    # Tighter than ``pad`` so the logo can sit closer to the gradient top edge.
                    _grad_pad = max(2, int(round(0.04 * float(g.cell))))
                    bottom_max = min(bottom_max, int(gy) - _grad_pad)
            except Exception:
                pass
            bottom_max = max(top_min + 8, min(int(DESIGN_H) - pad, bottom_max))

            # Columns 2.5–12.5 (10 cells wide, centered on the former 3–12 band): wider slot → larger TT.
            _tt_span_w = 10.0
            _tt_col_center = 7.5
            gx_tt, _gy_tt, gw_tt, _gh_tt = rect_for_span_at_cell(
                float(_tt_span_w),
                1.0,
                row_1based=1.0,
                col_1based=float(_tt_col_center) - 0.5 * float(_tt_span_w),
            )
            x0 = int(gx_tt)
            x1 = int(gx_tt) + int(gw_tt)
            y0 = max(0, top_min)
            y1 = bottom_max
            rw = int(x1 - x0)
            rh = int(y1 - y0)
            if rw < 48 or rh < 48 or x1 <= x0:
                _legacy = rect_for_span_top_right_at_cell(
                    14, 4, row_1based=2, col_right_1based=17.5
                )
                _rt = int(round(float(g.y0) + (2.5 - 1.0) * float(g.cell)))
                _rb = int(round(float(g.y0) + (6.0 - 1.0) * float(g.cell)))
                _rh = max(1, _rb - _rt)
                return (
                    int(_legacy[0]),
                    int(_rt),
                    int(_legacy[2]),
                    int(_rh),
                )
            return (x0, y0, rw, rh)

        def _paste_bgra_contain_on_design(
            canvas_bgr: np.ndarray,
            patch_bgra: np.ndarray | None,
            rect: tuple[int, int, int, int] | list[int],
        ) -> None:
            """Paste ``patch_bgra`` centered inside ``rect`` on a design-sized
            BGR canvas using uniform contain-fit (no crop)."""
            if patch_bgra is None or alpha_blend_bgra_over_bgr is None:
                return
            rx, ry, rw, rh = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
            if rw < 1 or rh < 1:
                return
            ph, pw = int(patch_bgra.shape[0]), int(patch_bgra.shape[1])
            if pw < 1 or ph < 1:
                return
            scale = min(rw / float(pw), rh / float(ph))
            nw = max(1, int(round(pw * scale)))
            nh = max(1, int(round(ph * scale)))
            rsz = cv2.resize(
                patch_bgra,
                (nw, nh),
                interpolation=cv_resize_interp(pw, ph, nw, nh),
            )
            ox = rx + (rw - nw) // 2
            oy = ry + (rh - nh) // 2
            dst_x0 = max(0, ox)
            dst_y0 = max(0, oy)
            dst_x1 = min(DESIGN_W, ox + nw)
            dst_y1 = min(DESIGN_H, oy + nh)
            if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
                return
            src_x0 = dst_x0 - ox
            src_y0 = dst_y0 - oy
            cw = dst_x1 - dst_x0
            ch = dst_y1 - dst_y0
            crop = rsz[src_y0 : src_y0 + ch, src_x0 : src_x0 + cw]
            sub = canvas_bgr[dst_y0:dst_y1, dst_x0:dst_x1]
            sub[:] = alpha_blend_bgra_over_bgr(sub, crop)

        def _current_app_display_name() -> str:
            """Human-readable name for the currently foregrounded streaming app."""
            label = str(streaming_badge_state.get("label") or "").strip()
            if label:
                return label
            lm = apple_tv_auto_state.get("last_metadata")
            if isinstance(lm, dict):
                for k in ("app_name", "app_id"):
                    v = str(lm.get(k) or "").strip()
                    if v:
                        return v
            return ""

        def _refresh_clock_patch_bgra() -> None:
            nonlocal clock_patch_bgra, _clock_patch_sig
            if clock_widget is None:
                return
            t = int(time.time())
            if status_bar_widget is not None:
                acc: tuple[int, int, int] | None = tuple(status_bar_widget.accent_bgr)
                clock_widget.set_shadow_accent_bgr(acc)
            else:
                acc = None
            if (
                clock_patch_bgra is not None
                and t == _clock_patch_sig[0]
                and acc == _clock_patch_sig[1]
            ):
                return
            clock_patch_bgra = clock_widget.bgra_patch().copy()
            _clock_patch_sig[0] = t
            _clock_patch_sig[1] = acc

        _playback_overlay_fast_sig: list[tuple[bool, bool, bool, bool] | None] = [None]

        def _maybe_blend_mic_visualizer(bgr_plane: np.ndarray) -> None:
            if not _PIGEON_EXT or _blend_mic_visualizer is None:
                return
            evm = _effective_display_view()
            if evm == DisplayView.TWO:
                mic_on = True
            elif evm in (DisplayView.THREE, DisplayView.FOUR):
                mic_on = False
            else:
                mic_on = not _backdrop_active_for_view()
            intro_t0 = mic_viz_intro_start_mono[0]
            # While the splash overlay is still up, ``intro_t0`` is unset; do **not** use
            # ``_pigeon_ui_started_mono`` here — that equals “intro already finished” and causes a full-EQ
            # flash, then a reset to t≈0 when splash lifts (jarring). Hold intro at t=0 until splash is gone.
            if intro_t0 is not None:
                landing_elapsed_s = time.monotonic() - intro_t0
            elif _PIGEON_EXT and startup_ph[0] is not None:
                landing_elapsed_s = 0.0
            else:
                landing_elapsed_s = time.monotonic() - _pigeon_ui_started_mono

            post_intro_descend: bool | None = None
            mic_effective = mic_on
            if evm not in (DisplayView.TWO, DisplayView.THREE, DisplayView.FOUR) and intro_t0 is not None:
                t_rel = float(time.monotonic() - intro_t0)
                if mic_viz_launch_descend_latched[0] is None and t_rel >= float(MIC_VIZ_INTRO_TOTAL_S):
                    mic_viz_launch_descend_latched[0] = 1 if _backdrop_active_for_view() else 0
                in_intro = t_rel < float(MIC_VIZ_INTRO_TOTAL_S)
                in_descent = (
                    mic_viz_launch_descend_latched[0] == 1
                    and t_rel >= float(MIC_VIZ_INTRO_TOTAL_S)
                    and (t_rel - float(MIC_VIZ_INTRO_TOTAL_S)) < float(MIC_VIZ_LAUNCH_DESCENT_S)
                )
                if in_intro or in_descent:
                    mic_effective = True
                if mic_viz_launch_descend_latched[0] == 1:
                    post_intro_descend = bool(
                        _backdrop_active_for_view()
                        or (
                            t_rel >= float(MIC_VIZ_INTRO_TOTAL_S)
                            and (t_rel - float(MIC_VIZ_INTRO_TOTAL_S))
                            < float(MIC_VIZ_LAUNCH_DESCENT_S)
                        )
                    )
                elif mic_viz_launch_descend_latched[0] == 0:
                    post_intro_descend = False
            if (
                evm not in (DisplayView.TWO, DisplayView.THREE, DisplayView.FOUR)
                and intro_t0 is not None
                and mic_viz_launch_descend_latched[0] == 1
                and (float(time.monotonic() - intro_t0))
                >= float(MIC_VIZ_INTRO_TOTAL_S) + float(MIC_VIZ_LAUNCH_DESCENT_S)
                and _backdrop_active_for_view()
            ):
                # Launch descent finished while a backdrop is still shown — hide EQ (incl. view 2) until backdrop clears.
                mic_effective = False

            _blend_mic_visualizer(
                bgr_plane,
                time.monotonic(),
                active=mic_effective,
                landing_elapsed_s=landing_elapsed_s,
                post_intro_backdrop_descend=post_intro_descend,
            )

        def compose_display_fast_no_grid(
            frame_bgr: np.ndarray | None,
            brightness: float,
            *,
            frame_is_display_sized: bool = False,
        ) -> np.ndarray:
            """Video at display size + poster/clock blits (no full design canvas). Used when developer grid is off."""
            assert _PIGEON_EXT
            if _effective_display_view() == DisplayView.TWO:
                from pigeon.image_ui_protocol import build_backdrop_design_layer_bgr

                dw, dh = display_dims[0], display_dims[1]
                cap_w, cap_h, use_cap = _composite_cap_dims(dw, dh)
                bm2 = _backdrop_bgr_for_view_two()
                if bm2 is not None:
                    bd2 = build_backdrop_design_layer_bgr(
                        bm2,
                        app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
                        app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
                    )
                    lit2 = _apply_brightness(bd2, brightness)
                    assert scale_cover_center_crop is not None
                    base2 = scale_cover_center_crop(lit2, cap_w, cap_h)
                else:
                    sb, sg, sr = get_stage_bgr()
                    base2 = np.empty((cap_h, cap_w, 3), dtype=np.uint8)
                    base2[:] = (sb, sg, sr)
                _maybe_blend_mic_visualizer(base2)
                if use_cap:
                    return _resize_bgr_to_dims(dw, dh, base2)
                return base2
            # View 1 pigeonFull: backdrop + title logo + theater widgets (fast cap path).
            # Variant-aware: V02/V03/V05 render here (post–v0.6.14 swap). V03 forces a black
            # base (BD missing); V05 replaces the TT image with generated title text in the
            # same rect that V02 uses for pigeonTMDB_TT.
            if _view_one_variant_uses_full_path():
                from pigeon.image_ui_protocol import build_backdrop_design_layer_bgr

                _set_playback_overlay_clock_saver_volume_flag()
                _warm_tmdb_logo_patch()
                dw, dh = display_dims[0], display_dims[1]
                cap_w, cap_h, use_cap = _composite_cap_dims(dw, dh)
                _vv_full = _current_view_one_variant()
                _vv_force_black_bd = bool(
                    ViewOneVariant is not None
                    and _vv_full == ViewOneVariant.V03
                )
                _vv_title_text_mode = bool(
                    ViewOneVariant is not None
                    and _vv_full == ViewOneVariant.V05
                )
                # viewOne.02 (post–v0.6.14 swap: the full-composition alternate) has
                # custom chrome positioning:
                #   • time right-aligned to the right edge of grid column 18
                #     (``col_right_1based=19.0`` in the top-right anchor API).
                #   • streaming appLogo left-aligned to the left edge of grid column 2
                #     (``col_right_1based = 2 + badge_span_w = 4.0``).
                # V03 (BD-missing) and V05 (TT-missing) keep the widgets' default
                # positions — the request was scoped to V02 only.
                _vv_full_is_v02 = bool(
                    ViewOneVariant is not None
                    and _vv_full == ViewOneVariant.V02
                )
                _v02_badge_dx = 0
                if (
                    _vv_full_is_v02
                    and rect_for_span_top_right_at_cell is not None
                    and playback_overlay_widget is not None
                ):
                    _b_span = getattr(playback_overlay_widget, "badge_span", (2, 1))
                    _b_row = float(getattr(playback_overlay_widget, "badge_row", 0.5))
                    _b_col_right_default = float(
                        getattr(playback_overlay_widget, "badge_top_right_col_1based", 18.0)
                    )
                    _b_col_right_v02 = float(_b_span[0]) + 2.0  # left edge of col 2
                    _x_default = rect_for_span_top_right_at_cell(
                        int(_b_span[0]),
                        int(_b_span[1]),
                        row_1based=_b_row,
                        col_right_1based=_b_col_right_default,
                    )[0]
                    _x_v02 = rect_for_span_top_right_at_cell(
                        int(_b_span[0]),
                        int(_b_span[1]),
                        row_1based=_b_row,
                        col_right_1based=_b_col_right_v02,
                    )[0]
                    _v02_badge_dx = int(_x_v02 - _x_default)
                bm2 = None if _vv_force_black_bd else _backdrop_bgr_for_view_two()
                now_bd = time.monotonic()
                if bm2 is not None:
                    bd2 = build_backdrop_design_layer_bgr(
                        bm2,
                        app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
                        app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
                    )
                    lit2 = _apply_brightness(bd2, brightness)
                    assert scale_cover_center_crop is not None
                    base2 = scale_cover_center_crop(lit2, cap_w, cap_h)
                    bdim_bd = _clock_saver_backdrop_brightness(now_bd)
                    if bdim_bd < 1.0 - 1e-6:
                        base2 = (base2.astype(np.float32) * bdim_bd).astype(np.uint8)
                elif _vv_force_black_bd:
                    base2 = np.zeros((cap_h, cap_w, 3), dtype=np.uint8)
                else:
                    sb, sg, sr = get_stage_bgr()
                    base2 = np.empty((cap_h, cap_w, 3), dtype=np.uint8)
                    base2[:] = (sb, sg, sr)
                # Mic/EQ visualizer is now drawn **above** the bottom gradient (see
                # per-branch blend points below). Keeping it here would let the
                # gradient (rows 7.5–8) dim the EQ bars — the user wants the
                # visualizer on top.
                _logo_w = _active_tmdb_logo_widget()
                # Precompute the variant TT (or V05 generated title text) patch + target rect but
                # defer the alpha-blend so it lands **above** the bottom gradient. Drawing the TT
                # before the bottom gradient would let ``playback_lower_gradient_bgra`` darken
                # or wash the logo — we want the gradient to affect the BD layer only, not the TT.
                _variant_tt_blit: tuple[np.ndarray, int, int, int, int] | None = None
                if _view_one_is_pigeon_simple():
                    # viewOne.videoContent_b: title on row 6.25 (Sharp Sans ExtraBold, audioConfig-sized).
                    _variant_tt_blit = None
                    if (
                        render_view_one_video_content_b_title_patch_bgra is not None
                        and alpha_blend_bgra_over_bgr is not None
                        and get_grid_geometry is not None
                    ):
                        _b_title = (active_tmdb_display_title or "").strip()
                        if _b_title:
                            _text_patch_b = render_view_one_video_content_b_title_patch_bgra(
                                _b_title
                            )
                            if _text_patch_b is not None:
                                g1 = get_grid_geometry()
                                _bph, _bpw = _text_patch_b.shape[:2]
                                wx = 0
                                wy = int(
                                    round(float(g1.y0) + (6.25 - 1.0) * float(g1.cell))
                                )
                                wy = max(0, min(wy, DESIGN_H - _bph))
                                ww, wh = int(DESIGN_W), int(_bph)
                                x, y, rw, rh = _design_rect_to_target(
                                    wx, wy, ww, wh, cap_w, cap_h
                                )
                                patch_b = cv2.resize(
                                    _text_patch_b,
                                    (rw, rh),
                                    interpolation=cv_resize_interp(
                                        _bpw, _bph, rw, rh
                                    ),
                                )
                                _variant_tt_blit = (
                                    patch_b,
                                    int(x),
                                    int(y),
                                    int(rw),
                                    int(rh),
                                )
                elif _effective_display_view() == DisplayView.THREE:
                    # viewThree.clock uses backdrop only in this path.
                    _variant_tt_blit = None
                elif _vv_title_text_mode and alpha_blend_bgra_over_bgr is not None:
                    # viewOne.05: generated title text reuses viewOne.02's TT rect
                    # (post–v0.6.14 swap: V02 is the full composition; V05 is the
                    # TT-missing "default" that substitutes rendered text).
                    assert get_grid_geometry is not None
                    g1 = get_grid_geometry()
                    # viewOne.videoContent_b TT band: y=[8.0, 8.5].
                    wh = max(1, int(round(0.5 * float(g1.cell))))
                    ww = max(1, int(round(DESIGN_W * 0.82)))
                    wy = int(round(g1.y0 + (8.0 - 1.0) * float(g1.cell)))
                    wx = int(round((DESIGN_W - ww) / 2.0))
                    wy = max(0, min(wy, DESIGN_H - wh))
                    wx = max(0, min(wx, DESIGN_W - ww))
                    _text_patch = (
                        render_ui_text_patch_bgra(active_tmdb_display_title or "", ww, wh)
                        if render_ui_text_patch_bgra is not None
                        else None
                    )
                    if _text_patch is not None:
                        x, y, rw, rh = _design_rect_to_target(wx, wy, ww, wh, cap_w, cap_h)
                        _th, _tw = _text_patch.shape[:2]
                        patch = cv2.resize(
                            _text_patch,
                            (rw, rh),
                            interpolation=cv_resize_interp(_tw, _th, rw, rh),
                        )
                        _variant_tt_blit = (patch, int(x), int(y), int(rw), int(rh))
                elif (
                    _logo_w is not None
                    and active_tmdb_title_key
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    # viewOne.videoContent_a (pigeonTMDB_BD + pigeonTMDB_TT): same maximal
                    # contain rect as the black simple path — largest uniform-fit without
                    # overlapping badge / clock / receiver stack / bottom chrome.
                    wx, wy, ww, wh = _view_one_video_content_a_tt_contain_rect_design()
                    _logo_patch = _logo_w.bgra_patch_for_title(
                        active_tmdb_title_key,
                        display_title=active_tmdb_display_title,
                        patch_wh=(int(ww), int(wh)),
                    )
                    x, y, rw, rh = _design_rect_to_target(wx, wy, ww, wh, cap_w, cap_h)
                    _lh, _lw = _logo_patch.shape[:2]
                    patch = cv2.resize(
                        _logo_patch,
                        (rw, rh),
                        interpolation=cv_resize_interp(_lw, _lh, rw, rh),
                    )
                    _variant_tt_blit = (patch, int(x), int(y), int(rw), int(rh))

                def _apply_variant_tt_blit_above_gradient(target: np.ndarray) -> None:
                    """Blit the deferred variant TT patch so it sits above the bottom gradient.

                    Keeps the BD layer (``pigeonTMDB_BD``) under the gradient while lifting the
                    TT (``pigeonTMDB_TT``) to the top of the chrome stack for readability.
                    """
                    if _variant_tt_blit is None or alpha_blend_bgra_over_bgr is None:
                        return
                    _p, _x, _y, _rw, _rh = _variant_tt_blit
                    sub = target[_y : _y + _rh, _x : _x + _rw]
                    sub[:] = alpha_blend_bgra_over_bgr(sub, _p)

                # Match the general fast path: lower gradient, clock saver when idle, else small clock widget.
                _blend_top_gradient_fast(base2, cap_w, cap_h)
                now_cs2 = time.monotonic()
                cs2 = _clock_saver_for_compose(now_cs2)
                if cs2:
                    # Saver mode has no bottom gradient, so blend the visualizer
                    # before the saver digits land. This keeps the saver clock
                    # legible on top while letting the bars play underneath.
                    _maybe_blend_mic_visualizer(base2)
                    if alpha_blend_bgra_over_bgr is not None:
                        acc_cs2 = (
                            tuple(status_bar_widget.accent_bgr)
                            if status_bar_widget is not None
                            else None
                        )
                        _cs_dim2 = _clock_saver_layer_opacity(now_cs2)
                        _clock_saver_dim_pre_digit_canvas(base2, _cs_dim2)
                        (time_bgra2, t_rect2), (date_bgra2, d_rect2) = clock_saver_composite_bgra(
                            shadow_bgr=acc_cs2,
                            layer_opacity=_cs_dim2,
                            time_layer_opacity=1.0,
                            date_layer_opacity=_cs_dim2,
                            date_anchor_row=CLOCK_ANCHOR_ROW,
                            date_anchor_col=CLOCK_ANCHOR_COL,
                        )
                        for cs_bgra2, (sx2, sy2, sw2, sh2) in (
                            (date_bgra2, d_rect2),
                            (time_bgra2, t_rect2),
                        ):
                            x2, y2, rw2, rh2 = _design_rect_to_target(sx2, sy2, sw2, sh2, cap_w, cap_h)
                            _ch2, _cw2 = cs_bgra2.shape[:2]
                            patch2 = cv2.resize(
                                cs_bgra2,
                                (rw2, rh2),
                                interpolation=cv_resize_interp(_cw2, _ch2, rw2, rh2),
                            )
                            sub2 = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                            sub2[:] = alpha_blend_bgra_over_bgr(sub2, patch2)
                        if (
                            (
                                playback_overlay_flags.get("clock_saver_volume_only")
                                or playback_overlay_flags.get("clock_saver_netflix_full_overlay")
                            )
                            and playback_overlay_blits
                            and alpha_blend_bgra_over_bgr is not None
                        ):
                            for pb2 in playback_overlay_blits:
                                x0b, y0b, wwb, whb = int(pb2.x), int(pb2.y), int(pb2.w), int(pb2.h)
                                x2, y2, rw2, rh2 = _design_rect_to_target(
                                    x0b, y0b, wwb, whb, cap_w, cap_h
                                )
                                _ph2, _pw2 = pb2.bgra.shape[:2]
                                patch_pb = cv2.resize(
                                    _clock_saver_dim_overlay_bgra(pb2.bgra, _cs_dim2),
                                    (rw2, rh2),
                                    interpolation=cv_resize_interp(_pw2, _ph2, rw2, rh2),
                                )
                                sub_pb = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                                sub_pb[:] = alpha_blend_bgra_over_bgr(sub_pb, patch_pb)
                else:
                    if (
                        playback_lower_gradient_bgra is not None
                        and alpha_blend_bgra_over_bgr is not None
                        and not _vv_is_music()
                    ):
                        gx2, gy2, gw2, gh2, grad_bgra2 = playback_lower_gradient_bgra(
                            gradient_bgr=tmdb_tt_gradient_bgr_holder[0]
                        )
                        x2, y2, rw2, rh2 = _design_rect_to_target(
                            gx2, gy2, gw2, gh2, cap_w, cap_h
                        )
                        _ghg2, _gwg2 = grad_bgra2.shape[:2]
                        patch_g2 = cv2.resize(
                            grad_bgra2,
                            (rw2, rh2),
                            interpolation=cv_resize_interp(_gwg2, _ghg2, rw2, rh2),
                        )
                        sub_g2 = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                        sub_g2[:] = alpha_blend_bgra_over_bgr(sub_g2, patch_g2)
                    # Viz blends AFTER the bottom gradient so the EQ bars sit
                    # above the gradient tint rather than being dimmed by it.
                    _maybe_blend_mic_visualizer(base2)
                    if _effective_display_view() != DisplayView.FOUR:
                        _refresh_clock_patch_bgra()
                        if (
                            clock_patch_bgra is not None
                            and clock_widget is not None
                            and alpha_blend_bgra_over_bgr is not None
                        ):
                            # viewOne.02 override: recompute the clock rect so its right edge
                            # anchors to the right side of grid column 18 (col_right=19.0).
                            # W/H stay the widget's configured span so ``clock_patch_bgra``
                            # (sized for the standard design_rect) still blits cleanly.
                            if (
                                _vv_full_is_v02
                                and rect_for_span_top_right_at_cell is not None
                            ):
                                _cw_span = getattr(clock_widget, "grid_span", (5, 1))
                                _cw_anchor_row = float(
                                    getattr(clock_widget, "grid_anchor", (0.5, 15.0))[0]
                                )
                                wx2, wy2, ww2, wh2 = rect_for_span_top_right_at_cell(
                                    int(_cw_span[0]),
                                    int(_cw_span[1]),
                                    row_1based=_cw_anchor_row,
                                    col_right_1based=19.0,
                                )
                            else:
                                dr2 = getattr(clock_widget, "design_rect", None)
                                wx2, wy2, ww2, wh2 = dr2() if callable(dr2) else (0, 0, 0, 0)
                            if ww2 >= 1 and wh2 >= 1:
                                x2, y2, rw2, rh2 = _design_rect_to_target(
                                    wx2, wy2, ww2, wh2, cap_w, cap_h
                                )
                                _kh2, _kw2 = clock_patch_bgra.shape[:2]
                                patch_ck = cv2.resize(
                                    clock_patch_bgra,
                                    (rw2, rh2),
                                    interpolation=cv_resize_interp(_kw2, _kh2, rw2, rh2),
                                )
                                sub_ck = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                                sub_ck[:] = alpha_blend_bgra_over_bgr(sub_ck, patch_ck)
                    if status_bar_blits and alpha_blend_bgra_over_bgr is not None:
                        for sb2 in status_bar_blits:
                            x0s, y0s, wws, whs = int(sb2.x), int(sb2.y), int(sb2.w), int(sb2.h)
                            x2, y2, rw2, rh2 = _design_rect_to_target(
                                x0s, y0s, wws, whs, cap_w, cap_h
                            )
                            _bhs, _bws = sb2.bgra.shape[:2]
                            patch_sb = cv2.resize(
                                sb2.bgra,
                                (rw2, rh2),
                                interpolation=cv_resize_interp(_bws, _bhs, rw2, rh2),
                            )
                            sub_sb = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                            sub_sb[:] = alpha_blend_bgra_over_bgr(sub_sb, patch_sb)
                    if playback_overlay_blits and alpha_blend_bgra_over_bgr is not None:
                        for pb2o in playback_overlay_blits:
                            x0p, y0p, wwp, whp = int(pb2o.x), int(pb2o.y), int(pb2o.w), int(pb2o.h)
                            # viewOne.02 override: translate the streaming-badge blit to
                            # left-align with grid column 2. The badge's W/H come from
                            # ``AudioConfig._build`` at its default col_right=18 anchor, so
                            # a pure x-shift is sufficient — no re-rasterization needed.
                            if (
                                _v02_badge_dx
                                and getattr(pb2o, "layer", "") == PATCH_LAYER_STREAMING_BADGE
                            ):
                                x0p += _v02_badge_dx
                            x2, y2, rw2, rh2 = _design_rect_to_target(
                                x0p, y0p, wwp, whp, cap_w, cap_h
                            )
                            _php, _pwp = pb2o.bgra.shape[:2]
                            patch_po = cv2.resize(
                                pb2o.bgra,
                                (rw2, rh2),
                                interpolation=cv_resize_interp(_pwp, _php, rw2, rh2),
                            )
                            sub_po = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                            sub_po[:] = alpha_blend_bgra_over_bgr(sub_po, patch_po)
                    if (
                        dev_phase == DevPhase.OFF
                        and location_toast_patch_bgra is not None
                        and alpha_blend_bgra_over_bgr is not None
                    ):
                        now_lt2 = time.monotonic()
                        ta2 = _location_toast_alpha(now_lt2)
                        if ta2 > 1e-6:
                            acc_lt = (
                                tuple(status_bar_widget.accent_bgr)
                                if status_bar_widget is not None
                                else None
                            )
                            patch_lt2, (lwx2, lwy2, lww2, lwh2) = location_toast_patch_bgra(
                                str(location_toast_state["text"]),
                                alpha=ta2,
                                shadow_bgr=acc_lt,
                                col_right_offset_cells=0.0,
                                row_offset_cells=0.0,
                                startup_top_left=False,
                            )
                            if patch_lt2 is not None:
                                x2, y2, rw2, rh2 = _design_rect_to_target(
                                    lwx2, lwy2, lww2, lwh2, cap_w, cap_h
                                )
                                _th2, _tw2 = patch_lt2.shape[:2]
                                patch_lt_r = cv2.resize(
                                    patch_lt2,
                                    (rw2, rh2),
                                    interpolation=cv_resize_interp(_tw2, _th2, rw2, rh2),
                                )
                                sub_lt = base2[y2 : y2 + rh2, x2 : x2 + rw2]
                                sub_lt[:] = alpha_blend_bgra_over_bgr(sub_lt, patch_lt_r)
                # TT (``pigeonTMDB_TT``) is the last thing blended in the pigeonFull path so it
                # sits **above** both the bottom gradient (drawn in the ``else`` branch above) and
                # the clock-saver layout (drawn in the ``if cs2:`` branch). The backdrop
                # (``pigeonTMDB_BD``) stays below the gradient as before — the gradient is still
                # visible over the BD, just not over the TT.
                _apply_variant_tt_blit_above_gradient(base2)
                if use_cap:
                    return _resize_bgr_to_dims(dw, dh, base2)
                return base2
            _set_playback_overlay_clock_saver_volume_flag()
            fast_sig = (
                bool(_backdrop_active_for_view()),
                _show_paused_row_overlay(),
                bool(playback_overlay_flags["clock_saver_volume_only"]),
                bool(playback_overlay_flags["clock_saver_netflix_full_overlay"]),
            )
            if playback_overlay_widget is not None and _playback_overlay_fast_sig[0] != fast_sig:
                _playback_overlay_fast_sig[0] = fast_sig
                _warm_playback_overlay_blits()
            dw, dh = display_dims[0], display_dims[1]
            cap_w, cap_h, use_cap = _composite_cap_dims(dw, dh)

            if frame_bgr is None or frame_bgr.size == 0:
                sb, sg, sr = get_stage_bgr()
                base = np.empty((cap_h, cap_w, 3), dtype=np.uint8)
                base[:] = (sb, sg, sr)
            else:
                lit = _apply_brightness(frame_bgr, brightness)
                if frame_is_display_sized:
                    if use_cap and (
                        int(lit.shape[1]) != cap_w or int(lit.shape[0]) != cap_h
                    ):
                        _lh, _lw = lit.shape[:2]
                        base = cv2.resize(
                            lit,
                            (cap_w, cap_h),
                            interpolation=cv_resize_interp(_lw, _lh, cap_w, cap_h),
                        )
                    else:
                        base = lit
                else:
                    fit = SceneFit(target_w=cap_w, target_h=cap_h) if use_cap else _disp_fit()
                    base = fit.scale_and_crop(lit)
            now_cs = time.monotonic()
            cs = _clock_saver_for_compose(now_cs)
            bdim = _clock_saver_backdrop_brightness(now_cs)
            if bdim < 1.0 - 1e-6:
                base = (base.astype(np.float32) * bdim).astype(np.uint8)
            # Composite order: clock saver / small clock / overlays sit above
            # the mic/EQ visualizer, which in turn sits above the bottom
            # gradient (so the gradient never dims the EQ bars). The viz is
            # blended per-branch below after the gradient call.
            _blend_top_gradient_fast(base, cap_w, cap_h)
            if cs:
                # Saver mode has no bottom gradient, so blend viz first; the
                # saver digits then land on top for legibility.
                _maybe_blend_mic_visualizer(base)
                if alpha_blend_bgra_over_bgr is not None:
                    acc_cs = (
                        tuple(status_bar_widget.accent_bgr)
                        if status_bar_widget is not None
                        else None
                    )
                    _cs_dim = _clock_saver_layer_opacity(now_cs)
                    _clock_saver_dim_pre_digit_canvas(base, _cs_dim)
                    (time_bgra, t_rect), (date_bgra, d_rect) = clock_saver_composite_bgra(
                        shadow_bgr=acc_cs,
                        layer_opacity=_cs_dim,
                        time_layer_opacity=1.0,
                        date_layer_opacity=_cs_dim,
                        date_anchor_row=CLOCK_ANCHOR_ROW,
                        date_anchor_col=CLOCK_ANCHOR_COL,
                    )
                    for cs_bgra, (sx, sy, sw, sh) in (
                        (date_bgra, d_rect),
                        (time_bgra, t_rect),
                    ):
                        x, y, rw, rh = _design_rect_to_target(sx, sy, sw, sh, cap_w, cap_h)
                        _ch, _cw = cs_bgra.shape[:2]
                        patch = cv2.resize(
                            cs_bgra, (rw, rh), interpolation=cv_resize_interp(_cw, _ch, rw, rh)
                        )
                        sub = base[y : y + rh, x : x + rw]
                        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                    if (
                        (
                            playback_overlay_flags.get("clock_saver_volume_only")
                            or playback_overlay_flags.get("clock_saver_netflix_full_overlay")
                        )
                        and playback_overlay_blits
                        and alpha_blend_bgra_over_bgr is not None
                    ):
                        for pb in playback_overlay_blits:
                            x0, y0, ww, wh = int(pb.x), int(pb.y), int(pb.w), int(pb.h)
                            x, y, rw, rh = _design_rect_to_target(x0, y0, ww, wh, cap_w, cap_h)
                            _ph, _pw = pb.bgra.shape[:2]
                            patch = cv2.resize(
                                _clock_saver_dim_overlay_bgra(pb.bgra, _cs_dim),
                                (rw, rh),
                                interpolation=cv_resize_interp(_pw, _ph, rw, rh),
                            )
                            sub = base[y : y + rh, x : x + rw]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
            else:
                if (
                    playback_lower_gradient_bgra is not None
                    and alpha_blend_bgra_over_bgr is not None
                    and not _vv_is_music()
                ):
                    gx, gy, gw, gh, grad_bgra = playback_lower_gradient_bgra(
                        gradient_bgr=tmdb_tt_gradient_bgr_holder[0]
                    )
                    x, y, rw, rh = _design_rect_to_target(gx, gy, gw, gh, cap_w, cap_h)
                    _gh, _gw = grad_bgra.shape[:2]
                    patch = cv2.resize(
                        grad_bgra, (rw, rh), interpolation=cv_resize_interp(_gw, _gh, rw, rh)
                    )
                    sub = base[y : y + rh, x : x + rw]
                    sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                # Viz blends AFTER the bottom gradient — EQ bars ride on top.
                _maybe_blend_mic_visualizer(base)
                if _effective_display_view() != DisplayView.FOUR:
                    _refresh_clock_patch_bgra()
                    if (
                        clock_patch_bgra is not None
                        and clock_widget is not None
                        and alpha_blend_bgra_over_bgr is not None
                    ):
                        dr = getattr(clock_widget, "design_rect", None)
                        wx, wy, ww, wh = dr() if callable(dr) else (0, 0, 0, 0)
                        if ww >= 1 and wh >= 1:
                            x, y, rw, rh = _design_rect_to_target(wx, wy, ww, wh, cap_w, cap_h)
                            _kh, _kw = clock_patch_bgra.shape[:2]
                            patch = cv2.resize(
                                clock_patch_bgra,
                                (rw, rh),
                                interpolation=cv_resize_interp(_kw, _kh, rw, rh),
                            )
                            sub = base[y : y + rh, x : x + rw]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if status_bar_blits and alpha_blend_bgra_over_bgr is not None:
                    for sb in status_bar_blits:
                        x0, y0, ww, wh = int(sb.x), int(sb.y), int(sb.w), int(sb.h)
                        x, y, rw, rh = _design_rect_to_target(x0, y0, ww, wh, cap_w, cap_h)
                        _bh, _bw = sb.bgra.shape[:2]
                        patch = cv2.resize(
                            sb.bgra,
                            (rw, rh),
                            interpolation=cv_resize_interp(_bw, _bh, rw, rh),
                        )
                        sub = base[y : y + rh, x : x + rw]
                        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if playback_overlay_blits and alpha_blend_bgra_over_bgr is not None:
                    for pb in playback_overlay_blits:
                        x0, y0, ww, wh = int(pb.x), int(pb.y), int(pb.w), int(pb.h)
                        x, y, rw, rh = _design_rect_to_target(x0, y0, ww, wh, cap_w, cap_h)
                        _ph, _pw = pb.bgra.shape[:2]
                        patch = cv2.resize(
                            pb.bgra,
                            (rw, rh),
                            interpolation=cv_resize_interp(_pw, _ph, rw, rh),
                        )
                        sub = base[y : y + rh, x : x + rw]
                        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if (
                    dev_phase == DevPhase.OFF
                    and location_toast_patch_bgra is not None
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    now_lt = time.monotonic()
                    ta = _location_toast_alpha(now_lt)
                    if ta > 1e-6:
                        acc = (
                            tuple(status_bar_widget.accent_bgr)
                            if status_bar_widget is not None
                            else None
                        )
                        patch_lt, (lwx, lwy, lww, lwh) = location_toast_patch_bgra(
                            str(location_toast_state["text"]),
                            alpha=ta,
                            shadow_bgr=acc,
                            col_right_offset_cells=0.0,
                            row_offset_cells=0.0,
                            startup_top_left=False,
                        )
                        if patch_lt is not None:
                            x, y, rw, rh = _design_rect_to_target(lwx, lwy, lww, lwh, cap_w, cap_h)
                            _th, _tw = patch_lt.shape[:2]
                            patch = cv2.resize(
                                patch_lt,
                                (rw, rh),
                                interpolation=cv_resize_interp(_tw, _th, rw, rh),
                            )
                            sub = base[y : y + rh, x : x + rw]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if (
                    _effective_display_view() not in (DisplayView.FOUR, DisplayView.TWO)
                    and (_logo_w := _active_tmdb_logo_widget()) is not None
                    and active_tmdb_title_key
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    if (
                        _effective_display_view() == DisplayView.ONE
                        and not _view_one_is_pigeon_poster()
                    ):
                        dwx, dwy, dww, dwh = _view_one_video_content_a_tt_contain_rect_design()
                    else:
                        dwx, dwy, dww, dwh = _logo_w.design_rect()
                    x, y, rw, rh = _design_rect_to_target(dwx, dwy, dww, dwh, cap_w, cap_h)
                    _logo_patch = _logo_w.bgra_patch_for_title(
                        active_tmdb_title_key,
                        display_title=active_tmdb_display_title,
                        patch_wh=(int(dww), int(dwh)),
                    )
                    _ph, _pw = int(_logo_patch.shape[0]), int(_logo_patch.shape[1])
                    if _pw >= 1 and _ph >= 1 and rw >= 1 and rh >= 1:
                        _sc = min(rw / float(_pw), rh / float(_ph))
                        _nw = max(1, int(round(_pw * _sc)))
                        _nh = max(1, int(round(_ph * _sc)))
                        patch = cv2.resize(
                            _logo_patch,
                            (_nw, _nh),
                            interpolation=cv_resize_interp(_pw, _ph, _nw, _nh),
                        )
                        _ox = x + (rw - _nw) // 2
                        _oy = y + (rh - _nh) // 2
                        _dx0 = max(0, _ox)
                        _dy0 = max(0, _oy)
                        _dx1 = min(cap_w, _ox + _nw)
                        _dy1 = min(cap_h, _oy + _nh)
                        if _dx1 > _dx0 and _dy1 > _dy0:
                            _sx0 = _dx0 - _ox
                            _sy0 = _dy0 - _oy
                            _cw = _dx1 - _dx0
                            _ch = _dy1 - _dy0
                            _crop = patch[_sy0 : _sy0 + _ch, _sx0 : _sx0 + _cw]
                            sub = base[_dy0:_dy1, _dx0:_dx1]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, _crop)
            if use_cap:
                return _resize_bgr_to_dims(dw, dh, base)
            return base

        def compose_display_from_source(
            frame_bgr: np.ndarray | None,
            brightness: float,
            *,
            show_grid: bool,
            frame_is_design_sized: bool = False,
            tmdb_logo_cover_design_xywh: tuple[int, int, int, int] | None = None,
        ) -> np.ndarray:
            """
            Build WINDOW_W×WINDOW_H output: scale **source** video to design, draw widgets, optionally grid,
            then scale down. Using the raw frame avoids letterboxing an already 800×480 image (which shifted
            the grid/poster and cropped them on the left). Developer grid mode uses uniform letterboxing so
            the full design width (including grid column 1) is visible on narrow windows.
            """
            assert _PIGEON_EXT
            assert scale_height_and_center_crop is not None
            assert scale_cover_center_crop is not None
            assert blend_overlay_bgr is not None
            assert build_stage_overlay_source_bgra is not None

            def _paste_tmdb_logo_uniform_cover_design(
                canvas_bgr: np.ndarray,
                logo_w,
                rx: int,
                ry: int,
                rw: int,
                rh: int,
            ) -> None:
                if (
                    logo_w is None
                    or rw < 1
                    or rh < 1
                    or not active_tmdb_title_key
                    or alpha_blend_bgra_over_bgr is None
                ):
                    return
                patch_bgra = logo_w.bgra_patch_for_title(
                    active_tmdb_title_key,
                    display_title=active_tmdb_display_title,
                    patch_wh=(rw, rh),
                )
                ph, pw = int(patch_bgra.shape[0]), int(patch_bgra.shape[1])
                if pw < 1 or ph < 1:
                    return
                # Uniform fit inside the grid box (no crop): largest scale where both dimensions fit;
                # centers the patch so ascenders / top caps are not clipped (cover would crop).
                scale_c = min(rw / float(pw), rh / float(ph))
                nw = max(1, int(round(pw * scale_c)))
                nh = max(1, int(round(ph * scale_c)))
                rsz = cv2.resize(
                    patch_bgra,
                    (nw, nh),
                    interpolation=cv_resize_interp(pw, ph, nw, nh),
                )
                ox = rx + (rw - nw) // 2
                oy = ry + (rh - nh) // 2
                dst_x0 = max(0, ox)
                dst_y0 = max(0, oy)
                dst_x1 = min(DESIGN_W, ox + nw)
                dst_y1 = min(DESIGN_H, oy + nh)
                if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
                    return
                src_x0 = dst_x0 - ox
                src_y0 = dst_y0 - oy
                cw = dst_x1 - dst_x0
                ch = dst_y1 - dst_y0
                crop2 = rsz[src_y0 : src_y0 + ch, src_x0 : src_x0 + cw]
                sub = canvas_bgr[dst_y0:dst_y1, dst_x0:dst_x1]
                sub[:] = alpha_blend_bgra_over_bgr(sub, crop2)

            def _paste_video_content_c_poster_above_top_gradient(canvas_bgr: np.ndarray) -> None:
                if (
                    not _view_one_is_pigeon_poster()
                    or _effective_display_view() != DisplayView.ONE
                    or alpha_blend_bgra_over_bgr is None
                    or get_grid_geometry is None
                ):
                    return
                g = get_grid_geometry()
                # Full-width band, vertically centered in rows 1→7.5 so the poster reads centered
                # on the canvas (not biased toward the top margin above row 1).
                top_y = int(round(g.y0 + (1.0 - 1.0) * float(g.cell)))
                bottom_y = int(round(g.y0 + (7.5 - 1.0) * float(g.cell)))
                poster_h = max(1, bottom_y - top_y)
                rect = (0, int(top_y), int(DESIGN_W), int(poster_h))
                patch_bgra = _styled_video_content_c_poster(_active_tmdb_poster_bgra())
                if patch_bgra is None:
                    return
                rx, ry, rw, rh = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
                ph, pw = int(patch_bgra.shape[0]), int(patch_bgra.shape[1])
                if rw < 1 or rh < 1 or pw < 1 or ph < 1:
                    return
                scale = min(rw / float(pw), rh / float(ph))
                nw = max(1, int(round(pw * scale)))
                nh = max(1, int(round(ph * scale)))
                rsz = cv2.resize(
                    patch_bgra,
                    (nw, nh),
                    interpolation=cv_resize_interp(pw, ph, nw, nh),
                )
                ox = rx + (rw - nw) // 2
                oy = ry + (rh - nh) // 2
                dst_x0 = max(0, ox)
                dst_y0 = max(0, oy)
                dst_x1 = min(DESIGN_W, ox + nw)
                dst_y1 = min(DESIGN_H, oy + nh)
                if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
                    return
                src_x0 = dst_x0 - ox
                src_y0 = dst_y0 - oy
                cw = dst_x1 - dst_x0
                ch = dst_y1 - dst_y0
                crop2 = rsz[src_y0 : src_y0 + ch, src_x0 : src_x0 + cw]
                sub = canvas_bgr[dst_y0:dst_y1, dst_x0:dst_x1]
                sub[:] = alpha_blend_bgra_over_bgr(sub, crop2)
            if frame_bgr is None or frame_bgr.size == 0:
                sb, sg, sr = get_stage_bgr()
                canvas = np.empty((DESIGN_H, DESIGN_W, 3), dtype=np.uint8)
                canvas[:] = (sb, sg, sr)
            else:
                lit = _apply_brightness(frame_bgr, brightness)
                if frame_is_design_sized:
                    canvas = lit
                else:
                    fit_d = SceneFit(target_w=DESIGN_W, target_h=DESIGN_H)
                    canvas = fit_d.scale_and_crop(lit)
            # All widget/grid math is in design pixels (DESIGN_W×DESIGN_H). If the base layer is off-size
            # (e.g. a bad master path), resize so overlays are not clipped on the left before scaling to the window.
            ch_can, cw_can = int(canvas.shape[0]), int(canvas.shape[1])
            if cw_can != DESIGN_W or ch_can != DESIGN_H:
                canvas = cv2.resize(
                    canvas,
                    (DESIGN_W, DESIGN_H),
                    interpolation=cv_resize_interp(cw_can, ch_can, DESIGN_W, DESIGN_H),
                )
            if not canvas.flags["C_CONTIGUOUS"]:
                canvas = np.ascontiguousarray(canvas)
            _set_playback_overlay_clock_saver_volume_flag()
            now_cs = time.monotonic()
            cs = _clock_saver_for_compose(now_cs) and not show_grid
            bdim_c = _clock_saver_backdrop_brightness(now_cs)
            if bdim_c < 1.0 - 1e-6:
                canvas = (canvas.astype(np.float32) * bdim_c).astype(np.uint8)
            # Layer order: top gradient first, then bottom gradient (in the
            # non-saver branch below), then the mic/EQ visualizer on top of
            # the gradient, then clock saver / clock widget / overlays on
            # top of the visualizer. See the saver branch for the no-gradient
            # variant.
            _blend_top_gradient_design(canvas)
            if cs:
                _maybe_blend_mic_visualizer(canvas)
                if alpha_blend_bgra_over_bgr is not None:
                    acc_cs = (
                        tuple(status_bar_widget.accent_bgr)
                        if status_bar_widget is not None
                        else None
                    )
                    _cs_dim_d = _clock_saver_layer_opacity(now_cs)
                    _clock_saver_dim_pre_digit_canvas(canvas, _cs_dim_d)
                    (time_bgra, t_rect), (date_bgra, d_rect) = clock_saver_composite_bgra(
                        shadow_bgr=acc_cs,
                        layer_opacity=_cs_dim_d,
                        time_layer_opacity=1.0,
                        date_layer_opacity=_cs_dim_d,
                        date_anchor_row=CLOCK_ANCHOR_ROW,
                        date_anchor_col=CLOCK_ANCHOR_COL,
                    )
                    for cs_bgra, (sx, sy, sw, sh) in (
                        (date_bgra, d_rect),
                        (time_bgra, t_rect),
                    ):
                        roi2 = canvas[sy : sy + sh, sx : sx + sw]
                        roi2[:] = alpha_blend_bgra_over_bgr(roi2, cs_bgra)
                    if playback_overlay_widget is not None and (
                        playback_overlay_flags.get("clock_saver_volume_only")
                        or playback_overlay_flags.get("clock_saver_netflix_full_overlay")
                    ):
                        ch, cw = canvas.shape[:2]
                        for p in playback_overlay_widget.design_blits():
                            x, y, w, h = p.x, p.y, p.w, p.h
                            if w < 1 or h < 1:
                                continue
                            x0 = max(0, x)
                            y0 = max(0, y)
                            x1 = min(cw, x + w)
                            y1 = min(ch, y + h)
                            if x0 >= x1 or y0 >= y1:
                                continue
                            sx0 = x0 - x
                            sy0 = y0 - y
                            roi = canvas[y0:y1, x0:x1]
                            src = _clock_saver_dim_overlay_bgra(p.bgra, _cs_dim_d)
                            patch = src[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
                            roi[:] = alpha_blend_bgra_over_bgr(roi, patch)
            else:
                if (
                    playback_lower_gradient_bgra is not None
                    and alpha_blend_bgra_over_bgr is not None
                    and not _vv_is_music()
                ):
                    gx, gy, gw, gh, grad_bgra = playback_lower_gradient_bgra(
                        gradient_bgr=tmdb_tt_gradient_bgr_holder[0]
                    )
                    sub = canvas[gy : gy + gh, gx : gx + gw]
                    sub[:] = alpha_blend_bgra_over_bgr(sub, grad_bgra)
                # Viz blends AFTER the bottom gradient — EQ bars ride on top.
                _maybe_blend_mic_visualizer(canvas)
                # viewOne.videoContent_c poster: sits above the top gradient and
                # below the nowPlaying widget (status bar + playback overlay).
                _paste_video_content_c_poster_above_top_gradient(canvas)
                if clock_widget is not None and _effective_display_view() != DisplayView.FOUR:
                    clock_widget.render(canvas)
                if status_bar_widget is not None:
                    status_bar_widget.render(canvas)
                if playback_overlay_widget is not None:
                    playback_overlay_flags["show_paused_row"] = _show_paused_row_overlay()
                    playback_overlay_widget.render(canvas)
                if (
                    dev_phase == DevPhase.OFF
                    and location_toast_patch_bgra is not None
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    now_lt = time.monotonic()
                    ta = _location_toast_alpha(now_lt)
                    if ta > 1e-6:
                        acc = (
                            tuple(status_bar_widget.accent_bgr)
                            if status_bar_widget is not None
                            else None
                        )
                        patch_lt, (lwx, lwy, lww, lwh) = location_toast_patch_bgra(
                            str(location_toast_state["text"]),
                            alpha=ta,
                            shadow_bgr=acc,
                            col_right_offset_cells=0.0,
                            row_offset_cells=0.0,
                            startup_top_left=False,
                        )
                        if patch_lt is not None:
                            sub = canvas[lwy : lwy + lwh, lwx : lwx + lww]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, patch_lt)
                _logo_w2 = _active_tmdb_logo_widget()
                if _logo_w2 is not None and _effective_display_view() not in (
                    DisplayView.FOUR,
                    DisplayView.TWO,
                    DisplayView.THREE,
                ) and not _view_one_is_pigeon_poster():
                    if tmdb_logo_cover_design_xywh is not None:
                        lx, ly, lw, lh = tmdb_logo_cover_design_xywh
                        _paste_tmdb_logo_uniform_cover_design(
                            canvas, _logo_w2, int(lx), int(ly), int(lw), int(lh)
                        )
                    elif (
                        _effective_display_view() == DisplayView.ONE
                        and active_tmdb_title_key
                    ):
                        _dwx, _dwy, _dww, _dwh = _view_one_video_content_a_tt_contain_rect_design()
                        _paste_tmdb_logo_uniform_cover_design(
                            canvas, _logo_w2, int(_dwx), int(_dwy), int(_dww), int(_dwh)
                        )
                    else:
                        _logo_w2.render(
                            canvas,
                            title_key_str=active_tmdb_title_key,
                            display_title=active_tmdb_display_title,
                        )
            if show_grid:
                ov = build_stage_overlay_source_bgra()
                canvas = blend_overlay_bgr(canvas, ov)
            tw, th = display_dims[0], display_dims[1]
            # Grid: uniform letterbox so narrow windows still show the full design width. Theater/backdrop:
            # cover-scale so the design fills the window (center-crop, no letterboxing).
            # Keep the same cover/crop presentation in grid mode so the overlay
            # sits on top of the "last seen" UI without re-framing the scene.
            _use_design_letterbox = False
            cw, ch, cap_down = _composite_cap_dims(tw, th)
            if cap_down:
                if _use_design_letterbox:
                    out = scale_uniform_letterbox(canvas, cw, ch)
                else:
                    out = scale_cover_center_crop(canvas, cw, ch)
                return _resize_bgr_to_dims(tw, th, out)
            if _use_design_letterbox:
                return scale_uniform_letterbox(canvas, tw, th)
            return scale_cover_center_crop(canvas, tw, th)

        def _collect_view_four_raw_title_lines() -> list[tuple[str, bool]]:
            """View 4: streaming label, rawTitle fields, last TMDb fetch (if any)."""
            rows: list[tuple[str, bool]] = []

            def _ln(s: str) -> None:
                rows.append((s, False))

            lm_rt = apple_tv_auto_state.get("last_metadata")
            _svc_label = str(streaming_badge_state.get("label") or "").strip()
            _svc_app = str(lm_rt.get("app_name") or "").strip() if isinstance(lm_rt, dict) else ""
            if _svc_label:
                _ln(f"streamingService={_svc_label!r}")
            elif _svc_app:
                _ln(f"streamingService={_svc_app!r}")

            if not isinstance(lm_rt, dict):
                _ln("rawTitle: (no last_metadata dict)")
            else:
                try:
                    from pigeon.raw_title import raw_title_from_metadata_dict

                    rt = raw_title_from_metadata_dict(lm_rt)
                    _ln(f"rawTitle.source={rt.source!r}")
                    for fn in (
                        "raw_title",
                        "raw_series_name",
                        "raw_artist",
                        "raw_album",
                        "raw_episode_title",
                        "raw_query",
                        "season_index",
                        "episode_index",
                        "layer_series_title",
                        "layer_series_number",
                        "layer_episode_number",
                        "layer_episode_title",
                        "media_type_label",
                    ):
                        _ln(f"rawTitle.{fn}={getattr(rt, fn)!r}")
                    if rt.notes:
                        _ln(f"rawTitle.notes={rt.notes!r}")
                    sig = rt.training_signature_normalized()
                    if sig:
                        _ln(f"rawTitle.training_signature_normalized={sig!r}")
                except Exception as e:
                    _ln(f"rawTitle err={e}")
            if isinstance(lm_rt, dict):
                _pp = str(lm_rt.get("prefer_pyatv_media") or "").strip().lower()
                if _pp in ("auto", "tv", "movie"):
                    _ln(f"metadata.prefer_pyatv_media={_pp!r}")
                _ip = str(lm_rt.get("inferred_prefer") or "").strip().lower()
                if _ip in ("auto", "tv", "movie"):
                    _ln(f"metadata.prefer_tmdb={_ip!r}")
            _ti = apple_tv_auto_state.get("last_tmdb_fetch_input")
            _tr = apple_tv_auto_state.get("last_tmdb_fetch_refined")
            _tp = apple_tv_auto_state.get("last_tmdb_fetch_prefer")
            if _ti is not None and str(_ti).strip():
                _ln(f"tmdbFetch.input_query={str(_ti)!r}")
            if _tr is not None and str(_tr).strip():
                _ln(f"tmdbFetch.refined_query={str(_tr)!r}")
            if _tp is not None and str(_tp).strip():
                _ln(f"tmdbFetch.prefer={str(_tp)!r}")
            rx_dbg = receiver_telnet_debug_holder[0] if receiver_telnet_debug_holder else {}
            if isinstance(rx_dbg, dict) and rx_dbg:
                _ln("denonTelnet (debug):")
                for key in ("SI", "MS", "DC", "PS_MULTEQ", "PS_DYNEQ", "PS_DYNVOL", "PS_REFLEV"):
                    val = str(rx_dbg.get(key) or "").strip()
                    if val:
                        _ln(f"  {key}={val!r}")
                raw_blob = str(rx_dbg.get("_raw") or "").strip()
                if raw_blob:
                    _ln("  _raw=(see receiver_denon_telnet dump)")
            return rows

        def _collect_view_four_source_lines() -> list[tuple[str, bool]]:
            """View 4 subview: best-effort file/stream stats from poll metadata + receiver text hints."""
            from math import gcd

            rows: list[tuple[str, bool]] = []

            def _ln(s: str, bold: bool = False) -> None:
                rows.append((s, bold))

            def _md_pick(md: dict[str, object], *keys: str) -> str | None:
                for k in keys:
                    if k not in md:
                        continue
                    v = md[k]
                    if v is None:
                        continue
                    if isinstance(v, (int, float)):
                        if isinstance(v, float) and v != v:
                            continue
                        t = str(int(v)) if float(v) == int(v) else str(v)
                    else:
                        t = str(v).strip()
                    if t:
                        return t
                return None

            def _aspect_from_wh(w_s: str | None, h_s: str | None) -> str | None:
                if not w_s or not h_s:
                    return None
                try:
                    wi = int(round(float(w_s)))
                    hi = int(round(float(h_s)))
                except (TypeError, ValueError):
                    return None
                if wi <= 0 or hi <= 0:
                    return None
                g = gcd(wi, hi)
                return f"{wi // g}:{hi // g}"

            md = apple_tv_auto_state.get("last_metadata")
            inc = str(receiver_overlay_state.get("incoming") or "").strip()
            cfg = str(receiver_overlay_state.get("config") or "").strip()
            rx_blob = f"{inc} {cfg}".strip()

            if not isinstance(md, dict):
                _ln("(no last_metadata dict)", False)
                return rows

            w = _md_pick(md, "video_width", "width", "source_width", "ImageWidth", "image_width")
            h = _md_pick(md, "video_height", "height", "source_height", "ImageHeight", "image_height")
            res_one = _md_pick(
                md,
                "video_resolution",
                "source_resolution",
                "resolution",
                "VideoResolution",
            )
            if res_one:
                _ln(f"Video source resolution: {res_one}", False)
            elif w and h:
                _ln(f"Video source resolution: {w}×{h}", False)
            elif w or h:
                _ln(f"Video source resolution: {w or '?'}×{h or '?'}", False)
            else:
                _ln("Video source resolution: —", False)

            ar = _md_pick(md, "aspect_ratio", "video_aspect_ratio", "AspectRatio", "DisplayAspectRatio")
            if not ar:
                ar = _aspect_from_wh(w, h)
            _ln(f"Video source aspect ratio: {ar or '—'}", False)

            _ln(
                "Video source color space: "
                + (
                    _md_pick(
                        md,
                        "color_space",
                        "color_primaries",
                        "VideoColorSpace",
                        "ColorSpace",
                        "colour_space",
                    )
                    or "—"
                ),
                False,
            )
            _ln(
                "Video source frame rate: "
                + (
                    _md_pick(
                        md,
                        "frame_rate",
                        "framerate",
                        "fps",
                        "video_frame_rate",
                        "FrameRate",
                    )
                    or "—"
                ),
                False,
            )
            _ln(
                "Video source bit depth: "
                + (_md_pick(md, "video_bit_depth", "bit_depth", "bits_per_pixel", "VideoBitDepth") or "—"),
                False,
            )
            _ln(
                "Video source codec: "
                + (_md_pick(md, "video_codec", "codec", "video_format", "VideoCodec", "format") or "—"),
                False,
            )
            _ln(
                "Video source wrapper: "
                + (_md_pick(md, "container", "wrapper", "mime_type", "MimeType", "file_extension") or "—"),
                False,
            )
            _ln(
                "Audio source wrapper: "
                + (_md_pick(md, "audio_container", "audio_wrapper", "AudioContainer") or "—"),
                False,
            )
            _ln(
                "Audio source bit depth: "
                + (_md_pick(md, "audio_bit_depth", "source_audio_bit_depth", "AudioBitDepth") or "—"),
                False,
            )
            _ln(
                "Audio source bit rate: "
                + (_md_pick(md, "audio_bit_rate", "source_audio_bit_rate", "AudioBitrate", "audio_bitrate") or "—"),
                False,
            )
            _ln(
                "Audio source codec: "
                + (_md_pick(md, "audio_codec", "audio_format", "AudioCodec", "AudioFormat") or "—"),
                False,
            )

            def _lpcm_vs_bitstream(blob: str, md2: dict[str, object]) -> str:
                ac = str(md2.get("audio_codec") or md2.get("audio_format") or "").lower()
                blob_l = blob.lower()
                joined = f"{ac} {blob_l}"
                if "pcm" in joined or "lpcm" in joined or "linear pcm" in joined:
                    return "LPCM/PCM (from metadata/receiver text)"
                if any(
                    x in joined
                    for x in (
                        "dolby",
                        "dts",
                        "truehd",
                        "true-hd",
                        "eac3",
                        "e-ac-3",
                        "atmos",
                        "bitstream",
                        "dd+",
                        "dtsx",
                    )
                ):
                    return "Compressed / bitstream (from metadata/receiver text)"
                if blob:
                    return "Unknown (see receiver lines below)"
                return "—"

            _ln(f"Audio source LPCM vs bitstream: {_lpcm_vs_bitstream(rx_blob, md)}", False)

            proto = _md_pick(md, "protocol")
            if proto:
                _ln(f"Poll protocol: {proto}", False)
            appn = str(md.get("app_name") or "").strip()
            appid = str(md.get("app_id") or "").strip()
            if appn or appid:
                _ln(f"App: {appn!r} id={appid!r}", False)

            if inc:
                _ln(f"Receiver incoming (raw): {inc}", False)
            if cfg:
                _ln(f"Receiver config (raw): {cfg}", False)

            known = {
                "query",
                "title",
                "artist",
                "series_name",
                "album",
                "media_type",
                "total_time",
                "position",
                "device_state",
                "inferred_prefer",
                "prefer_pyatv_media",
                "content_key",
                "app_name",
                "app_id",
                "volume_percent",
                "prefer",
            }
            extra_keys = [k for k in sorted(md.keys()) if k not in known and not str(k).startswith("_")]
            if extra_keys:
                _ln("— other metadata keys —", False)
                for k in extra_keys[:36]:
                    try:
                        vv = md[k]
                        rep = repr(vv)
                        if len(rep) > 140:
                            rep = rep[:137] + "..."
                    except Exception:
                        rep = "?"
                    _ln(f"  {k}={rep}", False)
                if len(extra_keys) > 36:
                    _ln(f"  … ({len(extra_keys) - 36} more keys)", False)
            return rows

        def _collect_view_four_playback_lines() -> list[tuple[str, bool]]:
            rows: list[tuple[str, bool]] = []

            def _ln(s: str, bold: bool = False) -> None:
                rows.append((s, bold))

            md_raw = apple_tv_auto_state.get("last_metadata")
            md = md_raw if isinstance(md_raw, dict) else None
            inc = str(receiver_overlay_state.get("incoming") or "").strip()
            cfg = str(receiver_overlay_state.get("config") or "").strip()
            vol_line = str(receiver_overlay_state.get("volume") or "").strip()
            blob = f"{inc} {cfg}".lower()

            def _channels_guess(s: str) -> str:
                if "7.1" in s or "7_1" in s:
                    return "7.1 (hint)"
                if "5.1" in s or "5_1" in s:
                    return "5.1 (hint)"
                if "2.0" in s or "stereo" in s or "2ch" in s:
                    return "2.0 / stereo (hint)"
                if "atmos" in s:
                    return "Atmos (hint)"
                return "—"

            fmt = "—"
            if md:
                mt = str(md.get("media_type") or "").strip()
                if mt:
                    fmt = mt
            if inc or cfg:
                fmt = f"{fmt} | receiver: {(inc + ' ' + cfg).strip()[:120]}"

            _ln(f"Audio playback format: {fmt}", False)

            _ln(
                "Audio playback bit rate: "
                + (
                    str(md.get("audio_playback_bit_rate")).strip()
                    if md and md.get("audio_playback_bit_rate") is not None
                    else "—"
                ),
                False,
            )
            _ln(
                "Audio playback bit depth: "
                + (
                    str(md.get("audio_playback_bit_depth")).strip()
                    if md and md.get("audio_playback_bit_depth") is not None
                    else "—"
                ),
                False,
            )
            _ln(f"Audio playback available channels: {_channels_guess(blob)}", False)
            _ln(f"Audio playback active channels: {_channels_guess(blob)}", False)

            if vol_line:
                scale = "dB scale" if ("db" in vol_line.lower() or re.search(r"-?\d+\.\d+\s*d", vol_line.lower())) else (
                    "0–100" if re.search(r"\b\d{1,3}\b", vol_line) and "%" not in vol_line and "db" not in vol_line.lower() else "receiver raw"
                )
                _ln(f"Audio playback volume: {vol_line}", False)
                _ln(f"Audio playback volume scale: {scale}", False)
            elif md and md.get("volume_percent") is not None:
                try:
                    vp = int(max(0, min(100, round(float(md["volume_percent"])))))
                    _ln(f"Audio playback volume: {vp}", False)
                    _ln("Audio playback volume scale: Apple TV 0–100", False)
                except (TypeError, ValueError):
                    _ln("Audio playback volume: —", False)
                    _ln("Audio playback volume scale: —", False)
            else:
                _ln("Audio playback volume: —", False)
                _ln("Audio playback volume scale: —", False)

            dw, dh = int(display_dims[0]), int(display_dims[1])
            _ln(f"Video playback resolution (window): {dw}×{dh}", False)

            cap_fps = None
            try:
                if cap is not None and cap.isOpened():
                    cf = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
                    if cf > 1.0:
                        cap_fps = cf
            except Exception:
                cap_fps = None
            if cap_fps is not None:
                _ln(f"Video capture nominal FPS: {cap_fps:.3g}", False)
            else:
                _ln("Video capture nominal FPS: —", False)

            ui_hz = 1000.0 / float(frame_interval_ms) if frame_interval_ms else 0.0
            _ln(f"UI composite cadence: ~{ui_hz:.2f} Hz (frame_interval_ms={frame_interval_ms})", False)

            _ln("Video playback color: —", False)
            _ln("Video playback video bit depth: —", False)
            _ln("Video playback bit depth: —", False)
            if md:
                ds = str(md.get("device_state") or "").strip()
                pos = md.get("position")
                tot = md.get("total_time")
                _ln(f"Device state: {ds or '—'}", False)
                _ln(f"Position / duration: {pos!r} / {tot!r}", False)
            return rows

        def _blend_view_four_debug(bgr: np.ndarray) -> np.ndarray:
            if _effective_display_view() != DisplayView.FOUR:
                return bgr
            out = bgr.copy()
            font = cv2.FONT_HERSHEY_SIMPLEX
            mx = 10
            my_top = 12
            my_bot = 10
            H, W = int(out.shape[0]), int(out.shape[1])
            max_w = max(24, W - 2 * mx)
            sub_i = max(0, min(2, int(view_four_subview_holder[0])))
            sub_titles = ("Title Info", "Source Info", "Playback Info")
            if sub_i == 0:
                raw_debug_lines = [(f"View 4 — {sub_titles[sub_i]}", True)] + _collect_view_four_raw_title_lines()
            elif sub_i == 1:
                raw_debug_lines = [(f"View 4 — {sub_titles[sub_i]}", True)] + _collect_view_four_source_lines()
            else:
                raw_debug_lines = [(f"View 4 — {sub_titles[sub_i]}", True)] + _collect_view_four_playback_lines()
            _any_bold = bool(raw_debug_lines)

            def _vf_thick(sc: float) -> int:
                return 2 if sc >= 0.48 else 1

            def _vf_wrap_paragraph(text: str, sc: float, thick: int) -> list[str]:
                t = str(text).replace("\n", " ").strip()
                if not t:
                    return []
                words = t.split()
                lines_out: list[str] = []
                cur: str | None = None
                for w in words:
                    trial = w if cur is None else f"{cur} {w}"
                    tw, _ = cv2.getTextSize(trial, font, sc, thick)[0]
                    if tw <= max_w:
                        cur = trial
                    else:
                        if cur is not None:
                            lines_out.append(cur)
                            cur = None
                        tw_w, _ = cv2.getTextSize(w, font, sc, thick)[0]
                        if tw_w <= max_w:
                            cur = w
                        else:
                            chunk = ""
                            for ch in w:
                                t2 = chunk + ch
                                tw2, _ = cv2.getTextSize(t2, font, sc, thick)[0]
                                if tw2 <= max_w:
                                    chunk = t2
                                else:
                                    if chunk:
                                        lines_out.append(chunk)
                                    chunk = ch
                            cur = chunk if chunk else None
                if cur is not None:
                    lines_out.append(cur)
                return lines_out

            def _vf_layout(sc: float) -> tuple[list[tuple[str, bool]], int, int, int]:
                thick_n = _vf_thick(sc)
                thick_layout = max(thick_n + 2, 3) if _any_bold else thick_n
                phys: list[tuple[str, bool]] = []
                for raw, is_bold in raw_debug_lines:
                    twrap = thick_layout if is_bold else thick_n
                    for pl in _vf_wrap_paragraph(raw, sc, twrap):
                        phys.append((pl, is_bold))
                if not phys:
                    phys = [("(no rawTitle lines yet)", False)]
                (_rw, th), bl = cv2.getTextSize("|pqgy", font, sc, thick_layout)
                line_step = max(th + 6, int(th + bl * 0.5) + 4)
                return phys, line_step, th, bl

            def _vf_fits(sc: float) -> bool:
                phys, line_step, th, bl = _vf_layout(sc)
                n = len(phys)
                need = my_top + th + (n - 1) * line_step + bl + my_bot
                return need <= H

            lo, hi = 0.52, min(2.15, max(0.75, H / 64.0))
            if not _vf_fits(lo):
                sc = lo
                while sc > 0.28 and not _vf_fits(sc):
                    sc -= 0.04
            else:
                for _ in range(32):
                    mid = (lo + hi) * 0.5
                    if _vf_fits(mid):
                        lo = mid
                    else:
                        hi = mid
                sc = lo

            thick_n = _vf_thick(sc)
            phys, line_step, th, _ = _vf_layout(sc)
            y = my_top + th
            color_dim = (220, 228, 238)
            color_bold = (255, 255, 255)
            for row, is_bold in phys:
                t_draw = max(thick_n + 2, 3) if is_bold else thick_n
                c = color_bold if is_bold else color_dim
                cv2.putText(out, row, (mx, y), font, sc, c, t_draw, cv2.LINE_AA)
                y += line_step
            return out

        def _compose_shown_frame(frame_bgr: np.ndarray | None, brightness: float) -> np.ndarray:
            def _view_one_dark_accent_bg_bgr() -> tuple[int, int, int]:
                """Darker variant of the current accent color for viewOne video a/c backgrounds.

                Stays black until ``pigeonTMDB_BD`` is ready (so the accent is actually
                sampled from a real backdrop, not the orange fallback).
                """
                if not _vv_has_tmdb_bd():
                    return (0, 0, 0)
                if status_bar_widget is None:
                    return (0, 0, 0)
                base = tuple(int(v) & 255 for v in status_bar_widget.accent_bgr)
                # Keep the hue but darken enough to sit behind TT/poster overlays.
                darken = 0.42
                return (
                    int(round(base[0] * darken)),
                    int(round(base[1] * darken)),
                    int(round(base[2] * darken)),
                )

            if _PIGEON_EXT and _effective_display_view() == DisplayView.FOUR:
                return _black_screen_bgr()
            if (
                _PIGEON_EXT
                and _view_one_is_pigeon_poster()
                and not _vv_is_music()
                and _vv_has_content_title()
            ):
                # viewOne.videoContent_c: black base + active TMDb poster only
                # (no pigeonTMDB_TT / no pigeonTMDB_BD), with the same chrome stack
                # as the other View One layouts. Poster occupies y=[0, top(row 7)].
                sb, sg, sr = _view_one_dark_accent_bg_bgr()
                black = np.empty((DESIGN_H, DESIGN_W, 3), dtype=np.uint8)
                black[:] = (sb, sg, sr)
                return compose_display_from_source(
                    black,
                    brightness,
                    show_grid=_design_grid_overlay_active(),
                    frame_is_design_sized=True,
                )
            if _PIGEON_EXT and _view_one_variant_uses_simple_path():
                sb, sg, sr = _view_one_dark_accent_bg_bgr()
                black = np.empty((DESIGN_H, DESIGN_W, 3), dtype=np.uint8)
                black[:] = (sb, sg, sr)
                sub2_logo_rect = _view_one_video_content_a_tt_contain_rect_design()
                # MediaType.Music override (viewOne.01): TMDb doesn't index music
                # tracks, so no pigeonTMDB_TT is available. Substitute a two-line
                # text patch (track title large; "Artist - Album" smaller beneath)
                # inside the same rect pigeonTMDB_TT would occupy. Short-circuits
                # the V# resolver dispatch below so Music content consistently
                # renders text regardless of which fallback variant would otherwise
                # apply. The single-small-line case in ``render_ui_music_text_patch_bgra``
                # gives the title ~76% of the box height and the subtitle the rest.
                if _vv_is_music() and render_ui_music_text_patch_bgra is not None:
                    _m_title, _m_subtitle = _vv_music_text_lines()
                    if _m_title or _m_subtitle:
                        _music_bgra = render_ui_music_text_patch_bgra(
                            _m_title,
                            _m_subtitle,
                            "",
                            int(sub2_logo_rect[2]),
                            int(sub2_logo_rect[3]),
                        )
                        if _music_bgra is not None:
                            _paste_bgra_contain_on_design(
                                black, _music_bgra, sub2_logo_rect
                            )
                            return compose_display_from_source(
                                black,
                                brightness,
                                show_grid=_design_grid_overlay_active(),
                                frame_is_design_sized=True,
                            )
                # Variant-aware TT-slot content. V01/V04 draw the real pigeonTMDB_TT via
                # compose_display_from_source (post–v0.6.14 swap: V01 is the TT-only default,
                # V04 is the BD-missing alternate); V06/.07/.08/.09 substitute a generated
                # patch (title text / appLogo / app name / pigeonTempLogo).
                _vv_simple = _current_view_one_variant()
                _vv_use_default_tt = ViewOneVariant is None or _vv_simple in (
                    ViewOneVariant.V01,
                    ViewOneVariant.V04,
                )
                if not _vv_use_default_tt:
                    _override_bgra = None
                    if _vv_simple == ViewOneVariant.V06 and render_ui_text_patch_bgra is not None:
                        _override_bgra = render_ui_text_patch_bgra(
                            active_tmdb_display_title or "",
                            int(sub2_logo_rect[2]),
                            int(sub2_logo_rect[3]),
                        )
                    elif _vv_simple == ViewOneVariant.V07:
                        _override_bgra = _resolve_streaming_app_logo_bgra()
                    elif _vv_simple == ViewOneVariant.V08 and render_ui_text_patch_bgra is not None:
                        _override_bgra = render_ui_text_patch_bgra(
                            _current_app_display_name(),
                            int(sub2_logo_rect[2]),
                            int(sub2_logo_rect[3]),
                        )
                    elif _vv_simple == ViewOneVariant.V09 and load_pigeon_temp_logo_bgra is not None:
                        # viewOne.startUp vs. viewOne.noContent
                        # ------------------------------------
                        # While Pigeon is still "starting up" — meaning either
                        # (a) the post-splash bars choreography is still playing
                        # (``mic_viz_intro_start_mono`` within ``MIC_VIZ_INTRO_TOTAL_S``),
                        # or (b) no Apple TV / Roku metadata poll has finished yet
                        # (``apple_tv_dashboard_track['last_poll_ok'] is None``) —
                        # the V09 logo slot loops ``pigeonAssets/pigeonStartup.mp4``
                        # inside the same ``sub2_logo_rect`` that the static
                        # AppLogo_Pigeon.png would occupy. The video plays at
                        # 100% opacity so the motion reads clearly.
                        #
                        # Once Pigeon is "fully up and running" (first poll has
                        # returned, successfully or otherwise) we fall through
                        # to the static AppLogo_Pigeon.png at 30% alpha — the
                        # viewOne.noContent look.
                        _startup_video_used = False
                        _intro_start_mono = mic_viz_intro_start_mono[0]
                        _first_poll_ok = apple_tv_dashboard_track.get("last_poll_ok")
                        _in_startup_window = (
                            _first_poll_ok is None
                            or (
                                _intro_start_mono is not None
                                and (time.monotonic() - float(_intro_start_mono))
                                < float(MIC_VIZ_INTRO_TOTAL_S)
                            )
                        )
                        if (
                            current_startup_bgra_frame is not None
                            and _intro_start_mono is not None
                            and _in_startup_window
                        ):
                            _intro_elapsed = time.monotonic() - float(_intro_start_mono)
                            if _intro_elapsed < 0.0:
                                _intro_elapsed = 0.0
                            _vid_bgra = current_startup_bgra_frame(
                                Path(_PROJECT_DIR) / "pigeonAssets",
                                _intro_elapsed,
                                loop=True,
                            )
                            if _vid_bgra is not None:
                                _override_bgra = _vid_bgra
                                _startup_video_used = True
                        if not _startup_video_used:
                            _override_bgra = load_pigeon_temp_logo_bgra(
                                Path(_PROJECT_DIR) / "pigeonAssets"
                            )
                            if _override_bgra is None:
                                print(
                                    "pigeon: pigeonAssets/App logos/AppLogo_Pigeon.png not found — "
                                    "viewOne.noContent / viewOne.startUp will render black only.",
                                    file=sys.stderr,
                                )
                            else:
                                # viewOne.noContent: Pigeon logo at 30% opacity.
                                # Multiply the alpha channel so the logo blends
                                # softly rather than showing solid over black.
                                _override_bgra = _override_bgra.copy()
                                _override_bgra[..., 3] = (
                                    _override_bgra[..., 3].astype(np.float32) * 0.30
                                ).clip(0, 255).astype(np.uint8)
                    _paste_bgra_contain_on_design(black, _override_bgra, sub2_logo_rect)
                    return compose_display_from_source(
                        black,
                        brightness,
                        show_grid=_design_grid_overlay_active(),
                        frame_is_design_sized=True,
                    )
                return compose_display_from_source(
                    black,
                    brightness,
                    show_grid=_design_grid_overlay_active(),
                    frame_is_design_sized=True,
                    tmdb_logo_cover_design_xywh=sub2_logo_rect,
                )
            # View 2 backdrop + visualizer-only is handled in ``compose_display_fast_no_grid``.
            # View 1 pigeonFull also uses that backdrop fast path.
            if (
                _backdrop_active_for_view()
                and backdrop_master_bgr is not None
                and _effective_display_view() != DisplayView.TWO
                and not _view_one_variant_uses_full_path()
            ):
                from pigeon.image_ui_protocol import build_backdrop_design_layer_bgr

                if not _PIGEON_EXT:
                    # Legacy path: use backdrop-only display if extension isn't available.
                    bd = build_backdrop_design_layer_bgr(
                        backdrop_master_bgr,
                        app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
                        app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
                    )
                    return compose_display_from_source(bd, brightness, show_grid=False, frame_is_design_sized=True)
                bd = build_backdrop_design_layer_bgr(
                    backdrop_master_bgr,
                    app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
                    app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
                )
                return compose_display_from_source(
                    bd,
                    brightness,
                    show_grid=_design_grid_overlay_active(),
                    frame_is_design_sized=True,
                )

            if not _PIGEON_EXT:
                if frame_bgr is None or frame_bgr.size == 0:
                    return _black_screen_bgr()
                lit = _apply_brightness(frame_bgr, brightness)
                dw, dh = display_dims[0], display_dims[1]
                cw, ch, cap_down = _composite_cap_dims(dw, dh)
                small = SceneFit(target_w=cw, target_h=ch).scale_and_crop(lit)
                if cap_down:
                    return _resize_bgr_to_dims(dw, dh, small)
                return small
            if _PIGEON_EXT and _design_grid_overlay_active():
                return compose_display_from_source(frame_bgr, brightness, show_grid=True)
            return compose_display_fast_no_grid(frame_bgr, brightness)

        if _PIGEON_EXT:
            _warm_status_bar_blits()
            _warm_playback_overlay_blits()

        # Display off: no landing art (black / stage composite only in render_once).
        if not scene_enabled:
            last_frame = None
            scaled_display = None
            scaled_version = 0

        command_entry_visible = False
        command_bar = tk.Frame(shell, bg="#1a1a1e", height=30)
        command_bar.pack_propagate(False)
        command_entry = tk.Entry(
            command_bar,
            bg="#2d2d32",
            fg="#f0f0f0",
            insertbackground="#f0f0f0",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#0a84ff",
            font=("Helvetica", 12),
        )
        command_entry.pack(fill=tk.BOTH, expand=True, padx=4, pady=3)

        def _ui_scale() -> float:
            dw, dh = display_dims[0], display_dims[1]
            return max(0.45, min(min(dw / float(WINDOW_W), dh / float(WINDOW_H)), 5.0))

        def _layout_chrome() -> None:
            dw, dh = display_dims[0], display_dims[1]
            _ui_scale()
            if command_entry_visible:
                place_command_bar()
            if dev_phase == DevPhase.SETTINGS:
                try:
                    root.after_idle(_settings_update_scrollregion)
                except Exception:
                    pass

        def place_command_bar() -> None:
            dw, dh = display_dims[0], display_dims[1]
            ui = _ui_scale()
            bar_h = max(24, int(32 * ui))
            yb = dh - bar_h - int(4 * ui)
            command_bar.place(x=0, y=yb, width=dw, height=bar_h)

        def hide_command_entry(_event=None) -> None:
            nonlocal command_entry_visible
            command_entry_visible = False
            command_bar.place_forget()
            try:
                if dev_phase == DevPhase.SETTINGS:
                    settings_frame.focus_set()
                else:
                    label.focus_set()
            except tk.TclError:
                pass

        def _apply_dev_phase_widgets() -> None:
            if dev_phase == DevPhase.SETTINGS:
                try:
                    label.pack_forget()
                except tk.TclError:
                    pass
                settings_frame.pack(fill=tk.BOTH, expand=True)
                try:
                    settings_frame.lift()
                except tk.TclError:
                    pass
                try:
                    root.after(200, _schedule_refresh_pairing_leds)
                except Exception:
                    pass
            else:
                try:
                    settings_frame.pack_forget()
                except tk.TclError:
                    pass
                label.pack(fill=tk.BOTH, expand=True)

        def _current_location_display_name() -> str:
            cid = (read_current_location_id() or "").strip()
            for L in read_all_locations_v2():
                if str(L.get("id") or "") == cid:
                    n = str(L.get("name") or "").strip()
                    return n or "Room"
            return "Location"

        def _location_toast_alpha(now: float) -> float:
            st = location_toast_state
            if not st["active"]:
                return 0.0
            elapsed = now - float(st["t0"])
            if elapsed < LOCATION_TOAST_FULL_S:
                return 1.0
            if elapsed < LOCATION_TOAST_FULL_S + LOCATION_TOAST_FADE_S:
                return max(0.0, 1.0 - (elapsed - LOCATION_TOAST_FULL_S) / LOCATION_TOAST_FADE_S)
            st["active"] = False
            return 0.0

        def _start_location_toast(*, startup: bool = False) -> None:
            nonlocal skip_cache
            if not _PIGEON_EXT:
                return
            st = location_toast_state
            st["text"] = _current_location_display_name()
            st["active"] = True
            st["t0"] = time.monotonic()
            st["startup_top_left"] = bool(startup)
            skip_cache = None

        def sync_developer_chrome() -> None:
            was_phase = prev_dev_phase_for_location_toast[0]
            _apply_dev_phase_widgets()
            _layout_chrome()
            if dev_phase == DevPhase.GRID:
                root.title(f"Pigeon {version_string()} — Developer mode (grid)")
                label.configure(
                    highlightthickness=3,
                    highlightbackground="#0a84ff",
                    highlightcolor="#0a84ff",
                )
            elif dev_phase == DevPhase.SETTINGS:
                root.title(f"Pigeon {version_string()} — Developer mode (settings)")
                try:
                    label.configure(highlightthickness=0)
                except tk.TclError:
                    pass
            else:
                root.title("")
                label.configure(highlightthickness=0)
                hide_command_entry()
            if dev_phase == DevPhase.SETTINGS:
                _settings_bind_wheel_globals()
                root.after_idle(_settings_update_scrollregion)
                root.after_idle(_refresh_match_quality_glance_label)
            else:
                _settings_unbind_wheel_globals()
            if command_entry_visible:
                place_command_bar()
                command_bar.lift()
            if _PIGEON_EXT and dev_phase == DevPhase.OFF and was_phase != DevPhase.OFF:
                # Keep the same launch placement after leaving Settings.
                _start_location_toast(startup=True)
            prev_dev_phase_for_location_toast[0] = dev_phase

        def toggle_play(_event=None) -> None:
            nonlocal playing, brightness_from, brightness_target, brightness_t0, brightness_duration_s
            _bump_pigeon_user_activity()
            if not scene_enabled or use_backdrop_scene or last_frame is None:
                return
            playing = not playing
            brightness_from = brightness_current
            # False → full brightness; True → slightly dimmed (inverse of old “video playing” semantics).
            brightness_target = LANDING_DIM_BRIGHTNESS if playing else LANDING_DISPLAY_BRIGHTNESS
            brightness_duration_s = (
                brightness_duration_up_s if brightness_target > brightness_from else brightness_duration_down_s
            )
            brightness_t0 = time.monotonic()

        def quit_app(_event=None) -> None:
            root.quit()

        def cycle_dev_phase(_event=None) -> str:
            nonlocal dev_phase, skip_cache
            _bump_pigeon_user_activity()
            if dev_phase == DevPhase.GRID:
                dev_phase = DevPhase.OFF
            elif dev_phase == DevPhase.OFF:
                dev_phase = DevPhase.SETTINGS
            else:
                dev_phase = DevPhase.OFF
            skip_cache = None
            sync_developer_chrome()
            return "break"

        def _open_landing_scene() -> bool:
            """Black landing page + centered logo; clears TMDb backdrop display flags."""
            nonlocal last_frame, scaled_display, scaled_version, frame_interval_ms, use_backdrop_scene, backdrop_master_bgr
            use_backdrop_scene = False
            backdrop_master_bgr = None
            last_frame = landing_scene_design_bgr
            frame_interval_ms = max(1, int(round(1000.0 / _default_render_fps())))
            if not _PIGEON_EXT:
                scaled_display = _disp_fit().scale_and_crop(last_frame)
            else:
                scaled_display = None
            scaled_version += 1
            return True

        def toggle_scene(_event=None, *, require_overlay: bool = True) -> None:
            nonlocal cap, scene_enabled, last_frame, scaled_display, scaled_version, skip_cache, black_photo, playing, frame_interval_ms, use_backdrop_scene, backdrop_master_bgr
            _bump_pigeon_user_activity()
            if require_overlay and not _design_grid_overlay_active():
                return

            if scene_enabled:
                playing = False
                scene_enabled = False
                use_backdrop_scene = False
                backdrop_master_bgr = None
            else:
                if not _open_landing_scene():
                    return
                scene_enabled = True

            _save_persisted_scene_enabled(scene_enabled)
            skip_cache = None

            if dev_phase == DevPhase.SETTINGS:
                sync_developer_chrome()
                return

            if not scene_enabled:
                if _PIGEON_EXT:
                    out_bgr = _compose_shown_frame(None, 1.0)
                    _update_label_photo_from_bgr(label, out_bgr, label_live_photo)
                else:
                    if black_photo is None:
                        black_photo = _bgr_to_tk_image(_black_screen_bgr())
                    label.configure(image=black_photo)
                    label.image = black_photo
            elif scaled_display is not None:
                if _PIGEON_EXT:
                    shown = _compose_shown_frame(last_frame, brightness_current)
                else:
                    shown = _apply_brightness(scaled_display, brightness_current)
                _update_label_photo_from_bgr(label, shown, label_live_photo)

        _last_overlay_mono = [0.0]
        _last_s_mono = [0.0]
        _last_f10_mono = [0.0]
        _last_tmdb_hotkey_mono = [0.0]
        _last_tmdb_quality_report_mono = [0.0]
        # Set True by ⌘⇧X / Ctrl+Shift+X; cleared when a successful TMDb populate is scored.
        tmdb_quality_error_flag: list[bool] = [False]
        # Last content event key that has already been scored for TMDb quality.
        tmdb_quality_last_scored_event_key: list[str] = [""]
        tmdb_quality_overlay_mode: list[str] = [""]
        tmdb_quality_overlay_t0: list[float] = [0.0]
        _last_tmdb_match_toggle_mono = [0.0]
        _last_space_mono = [0.0]
        _last_adv_shift_tab_mono = [0.0]

        def _trigger_tmdb_quality_toggle_overlay(mode: str) -> None:
            tmdb_quality_overlay_mode[0] = str(mode or "")
            tmdb_quality_overlay_t0[0] = time.monotonic()

        def _tmdb_quality_toggle_overlay_state(
            now_mono: float,
        ) -> tuple[tuple[int, int, int], float, str, int]:
            """Return (BGR color, alpha, caption, phase_key) for the toggle-confirmation X overlay."""
            mode = str(tmdb_quality_overlay_mode[0] or "")
            if mode not in ("flag", "undo"):
                return ((255, 255, 255), 0.0, "", 0)
            t0 = float(tmdb_quality_overlay_t0[0] or 0.0)
            dt = max(0.0, now_mono - t0)
            hold_primary = 1.0
            hold_secondary = 3.0
            fade_s = 0.8
            total = hold_primary + hold_secondary + fade_s
            if dt >= total:
                tmdb_quality_overlay_mode[0] = ""
                return ((255, 255, 255), 0.0, "", 0)
            if mode == "flag":
                c1 = (255, 255, 255)  # white first
                c2 = (0, 0, 255)  # red second
                text = "TMDB ERROR FLAGGED"
            else:
                c1 = (0, 0, 255)  # red first
                c2 = (255, 255, 255)  # white second
                text = "TMDB ERROR REPORT UNDONE"
            if dt < hold_primary:
                return (c1, 1.0, text, 1)
            if dt < (hold_primary + hold_secondary):
                return (c2, 1.0, text, 2)
            fade_t = (dt - hold_primary - hold_secondary) / max(1e-6, fade_s)
            alpha = max(0.0, 1.0 - float(fade_t))
            return (c2, alpha, text, 3)

        def _blend_tmdb_quality_toggle_overlay(
            frame_bgr: np.ndarray,
            *,
            color_bgr: tuple[int, int, int],
            alpha: float,
            caption: str,
        ) -> None:
            if frame_bgr is None or frame_bgr.size == 0:
                return
            a = max(0.0, min(1.0, float(alpha)))
            if a <= 1e-6:
                return
            h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
            if h < 8 or w < 8:
                return
            overlay = frame_bgr.copy()
            margin = max(16, int(round(min(w, h) * 0.24)))
            x0, y0 = margin, margin
            x1, y1 = max(x0 + 1, w - margin), max(y0 + 1, h - margin)
            thick = max(6, int(round(min(w, h) * 0.02)))
            cv2.line(overlay, (x0, y0), (x1, y1), color_bgr, thickness=thick, lineType=cv2.LINE_AA)
            cv2.line(overlay, (x0, y1), (x1, y0), color_bgr, thickness=thick, lineType=cv2.LINE_AA)
            if caption:
                fs = max(0.7, min(2.6, float(min(w, h)) / 520.0))
                txt_th = max(1, int(round(thick * 0.35)))
                tw, th = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, fs, txt_th)[0]
                tx = max(8, (w - tw) // 2)
                ty = min(h - 10, y1 + max(22, int(round(0.08 * h))))
                cv2.putText(
                    overlay,
                    caption,
                    (tx, ty),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    fs,
                    color_bgr,
                    txt_th,
                    cv2.LINE_AA,
                )
            cv2.addWeighted(overlay, a, frame_bgr, 1.0 - a, 0.0, dst=frame_bgr)

        def _blend_tmdb_quality_flag_badge(frame_bgr: np.ndarray) -> None:
            """Persistent bottom-right red X while the TMDb quality flag is active."""
            if frame_bgr is None or frame_bgr.size == 0:
                return
            h, w = int(frame_bgr.shape[0]), int(frame_bgr.shape[1])
            if h < 16 or w < 16:
                return
            size = max(16, int(round(min(w, h) * 0.06)))
            pad = max(10, int(round(min(w, h) * 0.025)))
            x1 = w - pad
            y1 = h - pad
            x0 = max(0, x1 - size)
            y0 = max(0, y1 - size)
            thick = max(2, int(round(size * 0.22)))
            # Draw directly on the destination frame to avoid per-tick full-frame copies.
            cv2.line(frame_bgr, (x0, y0), (x1, y1), (0, 0, 255), thickness=thick, lineType=cv2.LINE_AA)
            cv2.line(frame_bgr, (x0, y1), (x1, y0), (0, 0, 255), thickness=thick, lineType=cv2.LINE_AA)

        def try_cycle_dev_phase(event: tk.Event | None) -> str | None:
            """Debounced OFF↔SETTINGS cycle (mouse / F9); design grid overlay is key 5."""
            now = time.monotonic()
            if now - _last_overlay_mono[0] < 0.08:
                return "break"
            _last_overlay_mono[0] = now
            cycle_dev_phase()
            return "break"

        def on_tab_key(event: tk.Event) -> str | None:
            """Plain Tab: toggle Settings ↔ OFF (never enters Grid)."""
            if _widget_accepts_typing(event.widget):
                return None
            if getattr(event, "keysym", "") == "ISO_Left_Tab":
                return None
            st_tab = int(getattr(event, "state", 0))
            if st_tab & 0x0001:
                return None
            if st_tab & 0x0004:
                return None
            _bump_pigeon_user_activity(event)
            nonlocal dev_phase, skip_cache
            if dev_phase == DevPhase.SETTINGS:
                dev_phase = DevPhase.OFF
            else:
                dev_phase = DevPhase.SETTINGS
            skip_cache = None
            sync_developer_chrome()
            return "break"

        def on_shift_tab_dev_cycle(event: tk.Event) -> str | None:
            """Shift+Tab: toggle Settings ↔ off (same as Tab; grid overlay is key 5)."""
            if _widget_accepts_typing(event.widget):
                return None
            ks = getattr(event, "keysym", "") or ""
            st = int(getattr(event, "state", 0))
            if st & 0x0004:
                return None
            if ks != "ISO_Left_Tab" and not (ks == "Tab" and (st & 0x0001)):
                return None
            now = time.monotonic()
            if now - _last_overlay_mono[0] < 0.08:
                return "break"
            _last_overlay_mono[0] = now
            cycle_dev_phase()
            return "break"

        def on_ctrl_tab(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            if not (int(getattr(event, "state", 0)) & 0x0004):
                return None
            now = time.monotonic()
            if now - _last_overlay_mono[0] < 0.08:
                return "break"
            _last_overlay_mono[0] = now
            cycle_dev_phase()
            return "break"

        def on_s_key(event: tk.Event) -> str | None:
            keysym = (getattr(event, "keysym", "") or "").lower()
            ch = (getattr(event, "char", "") or "").lower()
            if keysym != "s" and ch != "s":
                return None
            if not _design_grid_overlay_active():
                return None
            now = time.monotonic()
            if now - _last_s_mono[0] < 0.08:
                return "break"
            _last_s_mono[0] = now
            toggle_scene(require_overlay=True)
            return "break"

        def apply_saved_tmdb_backdrop_to_display() -> None:
            """Apply last TMDb backdrop + title logo (same as F10’s backdrop step)."""
            nonlocal playing, backdrop_master_bgr, backdrop_app_logo_letterbox_fit, use_backdrop_scene, scene_enabled, last_frame, scaled_display, scaled_version, skip_cache, brightness_current, brightness_from, brightness_target, brightness_t0
            if saved_backdrop_master_bgr is None:
                return
            playing = False
            backdrop_master_bgr = saved_backdrop_master_bgr.copy()
            backdrop_app_logo_letterbox_fit = saved_backdrop_app_logo_letterbox_fit
            use_backdrop_scene = True
            scene_enabled = True
            last_frame = None
            if status_bar_widget is not None and status_bar_widget.set_accent_from_backdrop_bgr(
                backdrop_master_bgr
            ):
                _warm_status_bar_blits()
            if not _PIGEON_EXT:
                from pigeon.image_ui_protocol import backdrop_scene_bgr_for_display

                scaled_display = backdrop_scene_bgr_for_display(
                    backdrop_master_bgr,
                    display_dims[0],
                    display_dims[1],
                    app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
                    app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
                )
            else:
                scaled_display = None
            scaled_version += 1
            brightness_current = brightness_from = brightness_target = BACKDROP_BRIGHTNESS
            brightness_t0 = time.monotonic()
            _warm_tmdb_logo_patch()
            _save_persisted_scene_enabled(True)
            skip_cache = None
            _apply_netflix_backdrop_when_running()
            render_once()

        def f10_cycle_scene_grid() -> None:
            """
            Developer grid only: F10 cycles display on (landing) → off → backdrop (if saved) → landing.
            """
            nonlocal cap, scene_enabled, last_frame, scaled_display, scaled_version, skip_cache, playing, frame_interval_ms, use_backdrop_scene, backdrop_master_bgr, brightness_current, brightness_from, brightness_target, brightness_t0

            _bump_pigeon_user_activity()
            landing_on = scene_enabled and (not use_backdrop_scene) and last_frame is not None

            if use_backdrop_scene and backdrop_master_bgr is not None:
                if not _open_landing_scene():
                    scene_enabled = False
                    _save_persisted_scene_enabled(False)
                    skip_cache = None
                    render_once()
                    return
                scene_enabled = True
                playing = False
                brightness_current = brightness_from = brightness_target = LANDING_DISPLAY_BRIGHTNESS
                brightness_t0 = time.monotonic()
                _save_persisted_scene_enabled(True)
                skip_cache = None
                render_once()
                return

            if landing_on:
                playing = False
                scene_enabled = False
                use_backdrop_scene = False
                backdrop_master_bgr = None
                last_frame = None
                scaled_display = None
                _save_persisted_scene_enabled(False)
                skip_cache = None
                render_once()
                return

            if saved_backdrop_master_bgr is not None:
                apply_saved_tmdb_backdrop_to_display()
                return

            if not _open_landing_scene():
                scene_enabled = False
                _save_persisted_scene_enabled(False)
                skip_cache = None
                render_once()
                return
            scene_enabled = True
            _save_persisted_scene_enabled(True)
            skip_cache = None
            render_once()

        def on_f10_key(_event: tk.Event | None = None) -> str:
            now = time.monotonic()
            if now - _last_f10_mono[0] < 0.12:
                return "break"
            _last_f10_mono[0] = now
            if _design_grid_overlay_active():
                f10_cycle_scene_grid()
            else:
                toggle_scene(require_overlay=False)
            return "break"

        def on_click_focus(_event: tk.Event | None = None) -> None:
            _bump_pigeon_user_activity(_event)
            try:
                label.focus_set()
            except tk.TclError:
                try:
                    root.focus_set()
                except tk.TclError:
                    pass

        def on_double_click_scene(_event: tk.Event | None = None) -> None:
            toggle_scene(require_overlay=False)

        def show_command_entry(_event=None) -> None:
            nonlocal command_entry_visible
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS) and display_view_holder[0] != DisplayView.FIVE:
                return
            command_entry_visible = True
            place_command_bar()
            command_bar.lift()
            command_entry.focus_set()

            def _focus_cmd() -> None:
                try:
                    command_entry.focus_force()
                except tk.TclError:
                    try:
                        command_entry.focus_set()
                    except tk.TclError:
                        pass

            root.after_idle(_focus_cmd)

        _last_command_submit_mono = [0.0]

        def parse_tmdb_command_phrase(phrase: str) -> tuple[str, str]:
            """Return (query, prefer) with prefer one of auto | movie | tv."""
            p = phrase.strip()
            m_tv = re.match(r"(?i)^tv\s+(.+)$", p)
            if m_tv:
                return m_tv.group(1).strip(), "tv"
            m_mov = re.match(r"(?i)^movie\s+(.+)$", p)
            if m_mov:
                return m_mov.group(1).strip(), "movie"
            return p, "auto"

        def _escape_log_field(s: str | None) -> str:
            return (s or "").replace("\\", "\\\\").replace("\t", " ").replace("\r", " ").replace("\n", " ")

        def _append_tmdb_quality_event_report_log(
            *,
            outcome: str,
            title_key: str | None,
            display_title: str | None,
            msg_m: str,
        ) -> None:
            """Append one scored TMDb quality event line (SUCCESS/FAILURE) with metadata context."""
            log_p = pigeon_state_dir() / "tmdb_quality_event_reports.log"
            log_p.parent.mkdir(parents=True, exist_ok=True)
            md_raw = apple_tv_auto_state.get("last_metadata")
            raw_bits: list[str] = []
            app_bits: list[str] = []
            if isinstance(md_raw, dict):
                app_name = str(md_raw.get("app_name") or "").strip()
                app_id = str(md_raw.get("app_id") or "").strip()
                if app_name:
                    app_bits.append(f'app_name="{_escape_log_field(app_name)}"')
                if app_id:
                    app_bits.append(f'app_id="{_escape_log_field(app_id)}"')
                try:
                    from pigeon.raw_title import raw_title_from_metadata_dict

                    rt = raw_title_from_metadata_dict(md_raw)
                    if (rt.raw_title or "").strip():
                        raw_bits.append(f'raw_title="{_escape_log_field(rt.raw_title)}"')
                    if (rt.raw_series_name or "").strip():
                        raw_bits.append(f'raw_series_name="{_escape_log_field(rt.raw_series_name)}"')
                    if (rt.raw_query or "").strip():
                        raw_bits.append(f'raw_query="{_escape_log_field(rt.raw_query)}"')
                    if (rt.raw_episode_title or "").strip():
                        raw_bits.append(f'raw_episode_title="{_escape_log_field(rt.raw_episode_title)}"')
                    if (rt.layer_series_title or "").strip():
                        raw_bits.append(f'layer_series_title="{_escape_log_field(rt.layer_series_title)}"')
                    if not raw_bits:
                        raw_bits.append("(rawTitle layers empty for this snapshot)")
                except Exception:
                    t_fallback = str(md_raw.get("title") or "").strip()
                    q_fallback = str(md_raw.get("query") or "").strip()
                    raw_bits.append(
                        f'fallback_title="{_escape_log_field(t_fallback)}" query="{_escape_log_field(q_fallback)}"'
                    )
            else:
                raw_bits.append("(no last_metadata dict)")
            sb_label = str(streaming_badge_state.get("label") or "").strip()
            sb_filename = str(streaming_badge_state.get("filename") or "").strip()
            if sb_label:
                app_bits.append(f'streaming_service_label="{_escape_log_field(sb_label)}"')
            if sb_filename:
                app_bits.append(f'streaming_service_badge="{_escape_log_field(sb_filename)}"')
            if not app_bits:
                app_bits.append("(streaming_service unknown)")
            fetch_head = (msg_m or "").split("::", 1)[0].strip()
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            out_u = str(outcome or "").strip().upper()
            if out_u not in ("SUCCESS", "FAILURE"):
                out_u = "UNKNOWN"
            line = (
                f"{ts}\t{out_u}\t"
                f'tmdb_title_key="{_escape_log_field(title_key or "")}"\t'
                f'display_title="{_escape_log_field(display_title or "")}"\t'
                f'fetch_summary_head="{_escape_log_field(fetch_head)}"\t'
                f"{' '.join(app_bits)}\t"
                f"{' '.join(raw_bits)}\n"
            )
            with log_p.open("a", encoding="utf-8") as lf:
                lf.write(line)
            try:
                from pigeon.tmdb_desktop_report import append_tmdb_quality_row

                append_tmdb_quality_row(
                    outcome=out_u,
                    title_key=title_key,
                    display_title=display_title,
                    fetch_summary_head=fetch_head,
                    app_streaming_context=" ".join(app_bits),
                    raw_title_context=" ".join(raw_bits),
                )
            except Exception:
                pass
            if out_u == "FAILURE":
                try:
                    from pigeon.tmdb_desktop_report import append_tmdb_error_event

                    append_tmdb_error_event(
                        last_metadata=md_raw if isinstance(md_raw, dict) else None,
                        streaming_badge_state=streaming_badge_state,
                    )
                except Exception:
                    pass

        def spawn_tmdb_poster_fetch(query: str, *, prefer: str = "auto") -> None:
            """TMDb search + download + poster pipeline on a worker thread.

            Short-circuits for MediaType.Music: on viewOne.audioContent the
            only TT-rect substitute we render is the two-line text patch
            (track title + "Artist – Album"), so there is no consumer for a
            TMDb movie/TV backdrop or title treatment. Skipping the fetch
            here also avoids ~1–3 s of background network work per track
            change plus the misleading retry-log entries that a music title
            would otherwise generate against a TV/movie-only index.
            """
            from pigeon.tmdb_poster import is_degenerate_tmdb_query, refine_tmdb_search_query

            if _vv_is_music():
                # Clear any prior fetch breadcrumbs so the debug view doesn't
                # show stale values carried over from the previous track/video.
                apple_tv_auto_state["last_tmdb_fetch_input"] = None
                apple_tv_auto_state["last_tmdb_fetch_refined"] = None
                apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
                return

            q_in = (query or "").strip()
            q = refine_tmdb_search_query(q_in) or ""
            if not q:
                return
            if is_degenerate_tmdb_query(q):
                return
            apple_tv_auto_state["last_tmdb_fetch_input"] = q_in
            apple_tv_auto_state["last_tmdb_fetch_refined"] = q
            apple_tv_auto_state["last_tmdb_fetch_prefer"] = str(prefer or "auto").strip() or "auto"

            def finish_tmdb(ok_m: bool, msg_m: str, backdrop_master: np.ndarray | None = None) -> None:
                nonlocal skip_cache, cap, scene_enabled, last_frame, scaled_display, scaled_version, playing, use_backdrop_scene, backdrop_master_bgr, saved_backdrop_master_bgr, saved_backdrop_app_logo_letterbox_fit, backdrop_app_logo_letterbox_fit, brightness_current, brightness_from, brightness_target, brightness_t0, active_tmdb_title_key, active_tmdb_display_title, tmdb_logo_patch_bgra, tmdb_logo_app_fallback_active
                sys.stderr.write(f"pigeon: tmdb → {msg_m}\n")
                sys.stderr.flush()
                if not ok_m:
                    # No show title found: do not interrupt with an error dialog. Surface the
                    # streaming-app logo in the content-logo slot and leave the current scene
                    # alone — no new backdrop is enabled here (successful matches still get
                    # their own backdrop below).
                    sys.stderr.write(
                        "pigeon: TMDb search found no match — showing streaming app logo in "
                        "the content-logo slot (no backdrop).\n"
                    )
                    sys.stderr.flush()
                    try:
                        from pigeon.tmdb_desktop_report import append_tmdb_error_event

                        md_err = apple_tv_auto_state.get("last_metadata")
                        append_tmdb_error_event(
                            last_metadata=md_err if isinstance(md_err, dict) else None,
                            streaming_badge_state=streaming_badge_state,
                            supplemental_metadata=(
                                f"no_match refined_query={q!r} prefer={prefer!r} msg={msg_m!s}"
                            ),
                        )
                    except Exception:
                        pass
                    active_tmdb_title_key = None
                    active_tmdb_display_title = None
                    tmdb_logo_app_fallback_active = True
                    if tmdb_logo_widget is not None:
                        tmdb_logo_widget.clear_cache()
                    if tmdb_logo_widget_view_six is not None:
                        tmdb_logo_widget_view_six.clear_cache()
                    _warm_tmdb_logo_patch()
                    skip_cache = None
                    render_once()
                    if dev_phase == DevPhase.SETTINGS:
                        sync_developer_chrome()
                    return
                tmdb_logo_app_fallback_active = False
                # msg_m includes a prefix when successful: "<title_key>::<display_title>::<summary>"
                parts = msg_m.split("::", 2)
                if len(parts) >= 2:
                    active_tmdb_title_key = parts[0].strip() or None
                    active_tmdb_display_title = parts[1].strip() or None
                else:
                    active_tmdb_title_key = None
                    active_tmdb_display_title = None
                if tmdb_logo_widget is not None:
                    tmdb_logo_widget.clear_cache()
                if tmdb_logo_widget_view_six is not None:
                    tmdb_logo_widget_view_six.clear_cache()
                _warm_tmdb_logo_patch()
                bd_use = backdrop_master
                from_app_logo = False
                if bd_use is None:
                    bd_use = _backdrop_master_from_streaming_app_logo()
                    from_app_logo = bd_use is not None
                if bd_use is not None:
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
                        cap = None
                    backdrop_master_bgr = bd_use
                    saved_backdrop_master_bgr = np.asarray(bd_use, dtype=np.uint8).copy()
                    saved_backdrop_app_logo_letterbox_fit = from_app_logo
                    backdrop_app_logo_letterbox_fit = from_app_logo
                    use_backdrop_scene = True
                    scene_enabled = True
                    playing = False
                    last_frame = None
                    if not _PIGEON_EXT:
                        scaled_display = None
                    else:
                        scaled_display = None
                    scaled_version += 1
                    _save_persisted_scene_enabled(True)
                    # Backdrop is static image — not paused-video 0.3; use dedicated backdrop level.
                    brightness_current = brightness_from = brightness_target = BACKDROP_BRIGHTNESS
                    brightness_t0 = time.monotonic()
                    if not _apply_netflix_backdrop_when_running():
                        if status_bar_widget is not None:
                            bd_arr = np.asarray(backdrop_master_bgr, dtype=np.uint8)
                            if status_bar_widget.set_accent_from_backdrop_bgr(bd_arr):
                                _warm_status_bar_blits()
                                skip_cache = None
                # Match-quality counters: score only when TMDb material changes to a
                # new content event key (not on same-content retries/refetches).
                if active_tmdb_title_key:
                    try:
                        ev_key = str(apple_tv_auto_state.get("content_key") or "").strip()
                        if not ev_key:
                            # Fallback so manual fetches without poll metadata still have
                            # a deterministic event key.
                            qk = str(apple_tv_auto_state.get("query") or "").strip()
                            ev_key = f"{str(active_tmdb_title_key or '').strip()}::{qk}"
                        if ev_key and ev_key != str(tmdb_quality_last_scored_event_key[0] or ""):
                            had_qe = bool(tmdb_quality_error_flag[0])
                            tmdb_quality_error_flag[0] = False
                            cur_q = read_app_state()
                            s_q = int(cur_q.get("tmdb_quality_successes", 0) or 0)
                            f_q = int(cur_q.get("tmdb_quality_failures", 0) or 0)
                            if had_qe:
                                f_q += 1
                                try:
                                    _append_tmdb_quality_event_report_log(
                                        outcome="FAILURE",
                                        title_key=active_tmdb_title_key,
                                        display_title=active_tmdb_display_title,
                                        msg_m=msg_m,
                                    )
                                except Exception:
                                    pass
                            else:
                                s_q += 1
                                try:
                                    _append_tmdb_quality_event_report_log(
                                        outcome="SUCCESS",
                                        title_key=active_tmdb_title_key,
                                        display_title=active_tmdb_display_title,
                                        msg_m=msg_m,
                                    )
                                except Exception:
                                    pass
                            write_app_state(tmdb_quality_successes=s_q, tmdb_quality_failures=f_q)
                            tmdb_quality_last_scored_event_key[0] = ev_key
                    except Exception:
                        pass
                if dev_phase == DevPhase.SETTINGS:
                    sync_developer_chrome()

            def worker() -> None:
                try:
                    from pigeon.tmdb_poster import apply_tmdb_movie_query

                    ok_w, msg_w, bd_w = apply_tmdb_movie_query(q, prefer=prefer)  # type: ignore[arg-type]
                except Exception as e:
                    ok_w, msg_w, bd_w = False, str(e), None
                root.after(0, lambda o=ok_w, m=msg_w, b=bd_w: finish_tmdb(o, m, b))

            threading.Thread(target=worker, daemon=True).start()

        def _content_indicator_ok() -> bool:
            if not _PIGEON_EXT:
                return False
            if not current_apple_tv.get("identifier"):
                return False
            if apple_tv_busy["active"]:
                return False
            lf = apple_tv_dashboard_track.get("last_poll_ok")
            # Failed poll must not be masked by a stale latched query.
            if lf is False:
                return False
            q = apple_tv_auto_state.get("query")
            if q:
                return True
            return False

        def _advanced_feature_pipeline_ok() -> bool:
            """Stricter than the Content LED: last poll succeeded and metadata is active (not idle/unknown)."""
            if not _PIGEON_EXT:
                return False
            if not current_apple_tv.get("identifier"):
                return False
            if apple_tv_busy["active"]:
                return False
            if apple_tv_dashboard_track.get("last_poll_ok") is not True:
                return False
            lm = apple_tv_auto_state.get("last_metadata")
            if not isinstance(lm, dict) or _atv_metadata_is_content_idle(lm):
                return False
            return True

        def _read_tmdb_quality_counts() -> tuple[int, int]:
            if not _PIGEON_EXT:
                return (0, 0)
            try:
                st = read_app_state()
                s = int(st.get("tmdb_quality_successes", 0) or 0)
                f = int(st.get("tmdb_quality_failures", 0) or 0)
                return (s, f)
            except Exception:
                return (0, 0)

        def on_reset_tmdb_match_quality_stats() -> None:
            """Zero the Settings success/fail counters (state.json only). Logs and desktop reports unchanged."""
            if not _PIGEON_EXT:
                return
            try:
                write_app_state(tmdb_quality_successes=0, tmdb_quality_failures=0)
                match_quality_glance_sig[0] = ""
                _refresh_match_quality_glance_label()
            except Exception:
                pass

        def _format_tmdb_match_quality_glance(s: int, f: int) -> str:
            tot = s + f
            if tot <= 0:
                return "TMDb match quality  —  no scored events yet"
            ok_pct = 100.0 * float(s) / float(tot)
            return f"TMDb match quality   {s} ok   {f} fail   {tot} events   {ok_pct:.0f}% ok"

        def _refresh_match_quality_glance_label() -> None:
            if dev_phase != DevPhase.SETTINGS:
                return
            w = match_quality_glance_label_holder[0]
            if w is None:
                return
            try:
                s, f = _read_tmdb_quality_counts()
                txt = _format_tmdb_match_quality_glance(s, f)
                if match_quality_glance_sig[0] == txt:
                    return
                match_quality_glance_sig[0] = txt
                w.configure(text=txt)
            except tk.TclError:
                pass

        def _refresh_content_indicator() -> None:
            cv = content_indicator_cv_holder[0]
            if cv is not None:
                try:
                    _paint_boolean_led(cv, _content_indicator_ok())
                except tk.TclError:
                    pass
            _refresh_match_quality_glance_label()

        def _paint_pair_led(which: int, ok: bool | None) -> None:
            """Pairing LED: green=detected+compatible, amber=detected+unknown/incompatible, red=not detected/not compatible."""
            canvas = pairing_led_holder[which] if 0 <= which < len(pairing_led_holder) else None
            if canvas is None:
                return
            try:
                canvas.delete("all")
                if ok is True:
                    fill = "#1fcb5d"
                elif ok is None:
                    fill = "#f0ad4e"
                else:
                    fill = "#e74c3c"
                canvas.create_oval(2, 2, 12, 12, fill=fill, outline="#151518", width=1)
            except tk.TclError:
                pass

        def _paint_cred_led_canvas(canvas: tk.Canvas | None, ok: bool | None) -> None:
            """Canvas LED: green=detected+compatible, amber=detected+unknown/incompatible, red=not detected/not compatible."""
            if canvas is None:
                return
            try:
                canvas.delete("all")
                if ok is True:
                    fill = "#1fcb5d"
                elif ok is None:
                    fill = "#f0ad4e"
                else:
                    fill = "#e74c3c"
                canvas.create_oval(2, 2, 12, 12, fill=fill, outline="#151518", width=1)
            except tk.TclError:
                pass

        def _remove_saved_player_device(for_location_id: str | None = None) -> None:
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            if not messagebox.askyesno(
                "Remove Player",
                "Remove the saved Player device?\n\n"
                "Playback metadata stops using this Apple TV. "
                "pyatv credentials on this Mac are not deleted (use Reset to wipe those).",
                parent=root,
            ):
                return
            lid = (for_location_id or read_current_location_id() or "").strip()
            write_saved_streaming_device(None, for_location_id=lid or None)
            cur = read_current_location_id()
            if lid and cur and lid == cur:
                streaming_slot_holder[0] = None
                clear_last_apple_tv()
                current_apple_tv.clear()
                current_apple_tv.update(
                    {"identifier": "", "address": "", "name": "", "label": ""}
                )
                apple_tv_auto_state["content_key"] = None
                apple_tv_auto_state["tmdb_key"] = None
                apple_tv_auto_state["query"] = None
                apple_tv_auto_state["last_metadata"] = None
                apple_tv_auto_state["last_tmdb_fetch_input"] = None
                apple_tv_auto_state["last_tmdb_fetch_refined"] = None
                apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
                apple_tv_playback_clock.clear()
                apple_tv_playback_clock.update(
                    {
                        "has_sync": False,
                        "sync_mono": 0.0,
                        "sync_position": 0.0,
                        "live_mode": False,
                        "playing": False,
                        "latched_total": None,
                        "latched_content_key": None,
                        "last_reported_total": None,
                        "display_played_sec": None,
                        "trt_next_fire_mono": None,
                    }
                )
                apple_tv_dashboard_track["last_poll_ok"] = None
                apple_tv_dashboard_track["consecutive_fail"] = 0
                _sync_status_bar_visibility_for_playback(None)
            else:
                streaming_slot_holder[0] = read_saved_streaming_device()
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def _remove_streaming_device_at(for_location_id: str, index: int) -> None:
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            if not messagebox.askyesno(
                "Remove Player",
                "Remove this Player entry from this location?",
                parent=root,
            ):
                return
            lid = str(for_location_id or "").strip()
            remove_device_at_slot_index("streaming", int(index), for_location_id=lid or None)
            cur = read_current_location_id()
            remaining = read_saved_streaming_devices_all()
            if lid and cur and lid == cur:
                streaming_slot_holder[0] = read_saved_streaming_device()
                if not remaining:
                    clear_last_apple_tv()
                    current_apple_tv.clear()
                    current_apple_tv.update(
                        {"identifier": "", "address": "", "name": "", "label": ""}
                    )
                    apple_tv_auto_state["content_key"] = None
                    apple_tv_auto_state["tmdb_key"] = None
                    apple_tv_auto_state["query"] = None
                    apple_tv_auto_state["last_metadata"] = None
                    apple_tv_auto_state["last_tmdb_fetch_input"] = None
                    apple_tv_auto_state["last_tmdb_fetch_refined"] = None
                    apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
                    apple_tv_playback_clock.clear()
                    apple_tv_playback_clock.update(
                        {
                            "has_sync": False,
                            "sync_mono": 0.0,
                            "sync_position": 0.0,
                            "live_mode": False,
                            "playing": False,
                            "latched_total": None,
                            "latched_content_key": None,
                            "last_reported_total": None,
                            "display_played_sec": None,
                            "trt_next_fire_mono": None,
                        }
                    )
                    apple_tv_dashboard_track["last_poll_ok"] = None
                    apple_tv_dashboard_track["consecutive_fail"] = 0
                    _sync_status_bar_visibility_for_playback(None)
                if playback_overlay_widget is not None:
                    playback_overlay_widget.clear_cache()
            skip_cache = None
            try:
                render_once()
            except Exception:
                pass
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def _remove_receiver_device_at(for_location_id: str, index: int) -> None:
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            if not messagebox.askyesno(
                "Remove Receiver",
                "Remove this Receiver entry from this location?",
                parent=root,
            ):
                return
            lid = str(for_location_id or "").strip()
            remove_device_at_slot_index("av_receiver", int(index), for_location_id=lid or None)
            cur = read_current_location_id()
            if lid and cur and lid == cur:
                avr_slot_holder[0] = read_saved_av_receiver()
                if avr_slot_holder[0] is None:
                    clear_last_receiver()
                    receiver_http_host["host"] = ""
                else:
                    av2 = avr_slot_holder[0]
                    adr = str(av2.get("address") or "").strip()
                    if adr:
                        write_last_receiver(
                            host=adr,
                            name=str(av2.get("name") or "").strip() or None,
                            label=str(av2.get("label") or "").strip() or None,
                            device_id=str(av2.get("identifier") or "").strip() or None,
                        )
                        receiver_http_host["host"] = adr
                if playback_overlay_widget is not None:
                    playback_overlay_widget.clear_cache()
            skip_cache = None
            try:
                render_once()
            except Exception:
                pass
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def _remove_saved_receiver_device(for_location_id: str | None = None) -> None:
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            if not messagebox.askyesno(
                "Remove Receiver",
                "Remove the saved Receiver device and stop the overlay status poll for it?",
                parent=root,
            ):
                return
            lid = (for_location_id or read_current_location_id() or "").strip()
            write_saved_av_receiver(None, for_location_id=lid or None)
            cur = read_current_location_id()
            if lid and cur and lid == cur:
                avr_slot_holder[0] = None
                clear_last_receiver()
                receiver_http_host["host"] = ""
                if playback_overlay_widget is not None:
                    playback_overlay_widget.clear_cache()
                try:
                    _warm_playback_overlay_blits()
                except Exception:
                    pass
                nonlocal skip_cache
                skip_cache = None
                try:
                    render_once()
                except Exception:
                    pass
            else:
                avr_slot_holder[0] = read_saved_av_receiver()
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def _paired_box_close_button(parent: tk.Frame, bg: str, command: object) -> tk.Button:
            return tk.Button(
                parent,
                text="\u00d7",
                command=command,
                font=(_S, 14, "normal"),
                fg="#888",
                bg=bg,
                activebackground=bg,
                activeforeground="#f0f0f0",
                bd=0,
                padx=6,
                pady=0,
                highlightthickness=0,
                cursor="hand2",
            )

        def _settings_parse_device_rows(raw: object) -> list[dict[str, str]]:
            """Normalize a location slot (list or legacy dict) to filled device rows."""
            if isinstance(raw, list):
                out: list[dict[str, str]] = []
                for x in raw:
                    if not isinstance(x, dict):
                        continue
                    if str(x.get("identifier") or "").strip() and str(x.get("address") or "").strip():
                        out.append(dict(x))
                return out
            if isinstance(raw, dict):
                d = dict(raw)
                if str(d.get("identifier") or "").strip() and str(d.get("address") or "").strip():
                    return [d]
            return []

        def _settings_parse_receiver_rows(raw: object) -> list[dict[str, str]]:
            """Receivers: show any row with a host/IP (identifier optional for legacy)."""
            if isinstance(raw, list):
                return [
                    dict(x)
                    for x in raw
                    if isinstance(x, dict) and str(x.get("address") or "").strip()
                ]
            if isinstance(raw, dict) and str(raw.get("address") or "").strip():
                return [dict(raw)]
            return []

        def _remove_aux_slot_device_at(
            for_location_id: str,
            slot_key: str,
            index: int,
            *,
            role_title: str,
        ) -> None:
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            if not messagebox.askyesno(
                f"Remove {role_title}",
                f"Remove this {role_title} entry from this location?",
                parent=root,
            ):
                return
            lid = str(for_location_id or "").strip()
            remove_device_at_slot_index(slot_key, int(index), for_location_id=lid or None)
            _apply_persisted_location_to_runtime()
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()
            nonlocal skip_cache
            skip_cache = None
            try:
                render_once()
            except Exception:
                pass

        def _rebuild_paired_devices_panel() -> None:
            paired_ui_leds["remote"] = None
            paired_ui_leds["airplay"] = None
            paired_ui_leds["receiver"] = None
            paired_observed_led_rows.clear()
            paired_observed_led_last_state.clear()
            receiver_panel_led_holder[0] = None
            for ch in list(paired_devices_inner.winfo_children()):
                try:
                    ch.destroy()
                except tk.TclError:
                    pass
            locs = read_all_locations_v2()
            cur_id = read_current_location_id()
            if not locs:
                tk.Label(
                    paired_devices_inner,
                    text="No locations yet — use Find device to add a Player and pick a room.",
                    fg="#555",
                    bg="#111",
                    font=S_FONT_SMALL,
                ).pack(anchor=tk.W)
                return

            tk.Label(
                paired_devices_inner,
                text="Locations & saved devices",
                fg="#aaa",
                bg="#111",
                font=S_FONT_SEC,
            ).pack(anchor=tk.W, pady=(0, 4))

            # Per location: every saved role row is listed (same IP can appear as Player and TV, etc.).
            visible_locs: list[
                tuple[str, str, bool, dict[str, list[dict[str, str]]]]
            ] = []
            for loc in locs:
                loc_id = str(loc.get("id") or "")
                loc_name = str(loc.get("name") or "Room").strip()
                is_cur = bool(cur_id and loc_id == cur_id)
                slots: dict[str, list[dict[str, str]]] = {
                    "streaming": _settings_parse_device_rows(loc.get("streaming")),
                    "av_receiver": _settings_parse_receiver_rows(loc.get("av_receiver")),
                    "tv": _settings_parse_device_rows(loc.get("tv")),
                    "projector": _settings_parse_device_rows(loc.get("projector")),
                    "game": _settings_parse_device_rows(loc.get("game")),
                    "other": _settings_parse_device_rows(loc.get("other")),
                }
                if not any(slots.values()):
                    continue
                visible_locs.append((loc_id, loc_name, is_cur, slots))

            if not visible_locs:
                tk.Label(
                    paired_devices_inner,
                    text="No devices saved in any location yet — use Find device.",
                    fg="#555",
                    bg="#111",
                    font=S_FONT_SMALL,
                ).pack(anchor=tk.W)
                return

            loc_grid = tk.Frame(paired_devices_inner, bg="#111")
            loc_grid.pack(fill=tk.BOTH, expand=True)
            loc_grid.columnconfigure(0, weight=1, uniform="locpair")
            loc_grid.columnconfigure(1, weight=1, uniform="locpair")

            _aux_role_rows_ui: tuple[tuple[str, str, str, str, str], ...] = (
                ("tv", "TV", "#1a2520", "#3a5c4a", "#8fd4a8"),
                ("projector", "Projector", "#25201a", "#5c543a", "#d4c48f"),
                ("game", "Game", "#1a1f28", "#3a445c", "#9ab8ff"),
                ("other", "Other", "#222228", "#444454", "#c0c0d0"),
            )

            for idx, (loc_id, loc_name, is_cur, slots) in enumerate(visible_locs):
                stream_rows = slots["streaming"]
                av_rows = slots["av_receiver"]
                row, col = divmod(idx, 2)
                padx_g = (0, 5) if col == 0 else (5, 0)
                outer_bd = "#4a6a8a" if is_cur else "#3a3a44"
                outer = tk.Frame(
                    loc_grid,
                    bg="#15151c",
                    highlightthickness=1,
                    highlightbackground=outer_bd,
                )
                outer.grid(row=row, column=col, sticky="ew", padx=padx_g, pady=(0, 10))
                head_loc = tk.Frame(outer, bg="#15151c")
                head_loc.pack(fill=tk.X, padx=8, pady=(6, 4))
                sub = f"{loc_name}  (active)" if is_cur else loc_name
                tk.Label(
                    head_loc,
                    text=sub,
                    fg="#8ec8ff" if is_cur else "#aaa",
                    bg="#15151c",
                    font=S_FONT_CAP_BOLD,
                ).pack(side=tk.LEFT)

                inner = tk.Frame(outer, bg="#15151c")
                inner.pack(fill=tk.X, padx=6, pady=(0, 6))

                for si, st in enumerate(stream_rows):
                    nick = str(st.get("nickname") or "").strip()
                    nm = nick or str(st.get("label") or st.get("name") or "Device").strip()
                    ip = str(st.get("address") or "").strip()
                    slot_tag = f" #{si + 1}" if len(stream_rows) > 1 else ""
                    use_live = bool(is_cur and si == 0)
                    if row_is_playback_apple_tv(st):
                        grp = tk.Frame(
                            inner,
                            bg="#1a1a24",
                            highlightthickness=1,
                            highlightbackground="#333",
                        )
                        grp.pack(fill=tk.X, pady=(0, 6))

                        def _player_row(
                            parent: tk.Frame,
                            led_key: str,
                            subtitle: str,
                            *,
                            use_live_leds: bool,
                            grp_bg: str,
                            ip_s: str,
                        ) -> None:
                            line = tk.Frame(parent, bg=grp_bg)
                            line.pack(fill=tk.X, padx=8, pady=(2, 2))
                            cv = tk.Canvas(line, width=14, height=18, bg=grp_bg, highlightthickness=0, bd=0)
                            cv.pack(side=tk.LEFT)
                            # Never leave this blank while waiting for the first live poll.
                            _paint_cred_led_canvas(cv, False)
                            if use_live_leds:
                                paired_ui_leds[led_key] = cv
                            tk.Label(
                                line,
                                text=f"{subtitle}  ·  {ip_s}",
                                fg="#bbb",
                                bg=grp_bg,
                                font=S_FONT_SMALL,
                            ).pack(side=tk.LEFT, padx=(6, 0))

                        head = tk.Frame(grp, bg="#1a1a24")
                        head.pack(fill=tk.X, padx=8, pady=(6, 2))
                        _paired_box_close_button(
                            head,
                            "#1a1a24",
                            lambda lid=loc_id, ix=si: _remove_streaming_device_at(lid, ix),
                        ).pack(side=tk.RIGHT, padx=(8, 0))
                        tk.Label(head, text="Player", fg="#7eb8ff", bg="#1a1a24", font=S_FONT_CAP_BOLD).pack(
                            side=tk.LEFT
                        )
                        tk.Label(head, text=f"{slot_tag}  {nm}", fg="#ccc", bg="#1a1a24", font=S_FONT_SMALL).pack(
                            side=tk.LEFT
                        )
                        _player_row(
                            grp,
                            "remote",
                            "AppleTV Remote",
                            use_live_leds=use_live,
                            grp_bg="#1a1a24",
                            ip_s=ip,
                        )
                        _player_row(
                            grp,
                            "airplay",
                            "AppleTV AirPlay",
                            use_live_leds=use_live,
                            grp_bg="#1a1a24",
                            ip_s=ip,
                        )
                    else:
                        grp_o = tk.Frame(
                            inner,
                            bg="#1c1c20",
                            highlightthickness=1,
                            highlightbackground="#3a3a44",
                        )
                        grp_o.pack(fill=tk.X, pady=(0, 6))
                        head_o = tk.Frame(grp_o, bg="#1c1c20")
                        head_o.pack(fill=tk.X, padx=8, pady=(6, 6))
                        _paired_box_close_button(
                            head_o,
                            "#1c1c20",
                            lambda lid=loc_id, ix=si: _remove_streaming_device_at(lid, ix),
                        ).pack(side=tk.RIGHT, padx=(8, 0))
                        tk.Label(head_o, text="Player", fg="#7eb8ff", bg="#1c1c20", font=S_FONT_CAP_BOLD).pack(
                            side=tk.LEFT
                        )
                        line_o = tk.Frame(grp_o, bg="#1c1c20")
                        line_o.pack(fill=tk.X, padx=8, pady=(2, 6))
                        cv_o = tk.Canvas(line_o, width=14, height=18, bg="#1c1c20", highlightthickness=0, bd=0)
                        cv_o.pack(side=tk.LEFT)
                        _paint_cred_led_canvas(cv_o, False)
                        if is_cur:
                            paired_observed_led_rows.append((cv_o, str(loc_id), dict(st)))
                        tk.Label(
                            line_o,
                            text=f"{slot_tag}  {nm}  ·  {ip}",
                            fg="#aaa",
                            bg="#1c1c20",
                            font=S_FONT_SMALL,
                        ).pack(side=tk.LEFT, padx=(6, 0))
                for ai, av in enumerate(av_rows):
                    grp_r = tk.Frame(
                        inner,
                        bg="#221a22",
                        highlightthickness=1,
                        highlightbackground="#5a3d5a",
                    )
                    grp_r.pack(fill=tk.X, pady=(0, 2))
                    head_r = tk.Frame(grp_r, bg="#221a22")
                    head_r.pack(fill=tk.X, padx=8, pady=(6, 2))
                    _paired_box_close_button(
                        head_r,
                        "#221a22",
                        lambda lid=loc_id, ix=ai: _remove_receiver_device_at(lid, ix),
                    ).pack(side=tk.RIGHT, padx=(8, 0))
                    tk.Label(head_r, text="Receiver", fg="#c9a0ff", bg="#221a22", font=S_FONT_CAP_BOLD).pack(
                        side=tk.LEFT
                    )
                    rnick = str(av.get("nickname") or "").strip()
                    an = rnick or str(av.get("label") or av.get("name") or "Receiver").strip()
                    aip = str(av.get("address") or "").strip()
                    rtag = f" #{ai + 1}" if len(av_rows) > 1 else ""
                    line = tk.Frame(grp_r, bg="#221a22")
                    line.pack(fill=tk.X, padx=8, pady=(2, 6))
                    cv_r = tk.Canvas(line, width=14, height=18, bg="#221a22", highlightthickness=0, bd=0)
                    cv_r.pack(side=tk.LEFT)
                    if is_cur and ai == 0:
                        paired_ui_leds["receiver"] = cv_r
                        receiver_panel_led_holder[0] = cv_r
                        _paint_boolean_led(cv_r, False)
                    else:
                        _paint_cred_led_canvas(cv_r, False)
                    tk.Label(
                        line,
                        text=f"{rtag}  {an}  ·  {aip}",
                        fg="#bbb",
                        bg="#221a22",
                        font=S_FONT_SMALL,
                    ).pack(side=tk.LEFT, padx=(6, 0))

                for slot_key, role_title, bg_c, bd_c, title_c in _aux_role_rows_ui:
                    aux_rows = slots.get(slot_key) or []
                    for ri, arow in enumerate(aux_rows):
                        nick_a = str(arow.get("nickname") or "").strip()
                        nm_a = nick_a or str(arow.get("label") or arow.get("name") or role_title).strip()
                        ip_a = str(arow.get("address") or "").strip()
                        tag_a = f" #{ri + 1}" if len(aux_rows) > 1 else ""
                        grp_a = tk.Frame(
                            inner,
                            bg=bg_c,
                            highlightthickness=1,
                            highlightbackground=bd_c,
                        )
                        grp_a.pack(fill=tk.X, pady=(0, 4))
                        head_a = tk.Frame(grp_a, bg=bg_c)
                        head_a.pack(fill=tk.X, padx=8, pady=(6, 6))
                        _paired_box_close_button(
                            head_a,
                            bg_c,
                            lambda lid=loc_id, sk=slot_key, ix=ri, rt=role_title: _remove_aux_slot_device_at(
                                lid, sk, ix, role_title=rt
                            ),
                        ).pack(side=tk.RIGHT, padx=(8, 0))
                        tk.Label(
                            head_a,
                            text=role_title,
                            fg=title_c,
                            bg=bg_c,
                            font=S_FONT_CAP_BOLD,
                        ).pack(side=tk.LEFT)
                        line_a = tk.Frame(grp_a, bg=bg_c)
                        line_a.pack(fill=tk.X, padx=8, pady=(2, 6))
                        cv_a = tk.Canvas(line_a, width=14, height=18, bg=bg_c, highlightthickness=0, bd=0)
                        cv_a.pack(side=tk.LEFT)
                        _paint_cred_led_canvas(cv_a, False)
                        if is_cur:
                            paired_observed_led_rows.append((cv_a, str(loc_id), dict(arow)))
                        tk.Label(
                            line_a,
                            text=f"{tag_a}  {nm_a}  ·  {ip_a}",
                            fg="#ccc",
                            bg=bg_c,
                            font=S_FONT_SMALL,
                        ).pack(side=tk.LEFT, padx=(6, 0))

        def describe_current_apple_tv(*, suffix: str | None = None) -> None:
            if current_apple_tv.get("name"):
                play = f'Playback: {current_apple_tv["name"]}'
            elif current_apple_tv.get("label"):
                play = f'Playback: {current_apple_tv["label"]}'
            else:
                play = "Playback: none"
            rh = str(receiver_http_host.get("host") or "").strip()
            if rh:
                lr = read_last_receiver()
                nm = str(lr.get("name") or lr.get("label") or rh).strip() or rh
                ov = f"Overlay: {nm}"
            else:
                ov = "Overlay: none"
            base = f"{play}  ·  {ov}"
            if suffix:
                base = f"{base} ({suffix})"
            apple_tv_status_var.set(base)
            _refresh_content_indicator()

        def set_apple_tv_controls_enabled(enabled: bool) -> None:
            state = tk.NORMAL if enabled else tk.DISABLED
            try:
                find_device_btn.configure(state=state)
                _m = tmdb_adv_manual_btn_holder[0]
                if _m is not None:
                    _m.configure(state=state)
                _r = tmdb_adv_report_btn_holder[0]
                if _r is not None:
                    _r.configure(state=state)
                purge_image_media_btn.configure(state=state)
                _fdb = settings_footer_debug_holder[0]
                if _fdb is not None:
                    _fdb.configure(state=state)
                _frb = settings_footer_reset_holder[0]
                if _frb is not None:
                    _frb.configure(state=state)
                root.configure(cursor="" if enabled else "watch")
            except tk.TclError:
                pass

        def begin_apple_tv_operation(status_suffix: str) -> bool:
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return False
            apple_tv_busy["active"] = True
            set_apple_tv_controls_enabled(False)
            describe_current_apple_tv(suffix=status_suffix)
            return True

        def end_apple_tv_operation(*, suffix: str | None = None) -> None:
            apple_tv_busy["active"] = False
            set_apple_tv_controls_enabled(True)
            describe_current_apple_tv(suffix=suffix)

        def set_current_apple_tv(row: dict[str, str], *, persist: bool) -> None:
            nonlocal last_atv_interaction_mono, _atv_ix_sig_ds, _atv_ix_sig_ck
            nonlocal _atv_ix_pos, _atv_ix_pos_mono, _atv_ix_extrap_playing, _atv_ix_prev_idle
            current_apple_tv.clear()
            current_apple_tv.update(
                {
                    "identifier": row.get("identifier", ""),
                    "address": row.get("address", ""),
                    "name": row.get("name", ""),
                    "label": row.get("label", ""),
                }
            )
            if persist:
                write_last_apple_tv(
                    identifier=row.get("identifier", ""),
                    address=row.get("address", ""),
                    name=row.get("name"),
                    label=row.get("label"),
                )
            apple_tv_auto_state["content_key"] = None
            apple_tv_auto_state["tmdb_key"] = None
            apple_tv_auto_state["query"] = None
            apple_tv_auto_state["last_metadata"] = None
            apple_tv_auto_state["last_tmdb_fetch_input"] = None
            apple_tv_auto_state["last_tmdb_fetch_refined"] = None
            apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
            apple_tv_playback_clock.clear()
            apple_tv_playback_clock.update(
                {
                    "has_sync": False,
                    "sync_mono": 0.0,
                    "sync_position": 0.0,
                    "live_mode": False,
                    "playing": False,
                    "latched_total": None,
                    "latched_content_key": None,
                    "last_reported_total": None,
                    "display_played_sec": None,
                    "trt_next_fire_mono": None,
                }
            )
            apple_tv_dashboard_track["last_poll_ok"] = None
            apple_tv_dashboard_track["consecutive_fail"] = 0
            last_atv_interaction_mono = 0.0
            _atv_ix_sig_ds = ""
            _atv_ix_sig_ck = None
            _atv_ix_pos = None
            _atv_ix_pos_mono = time.monotonic()
            _atv_ix_extrap_playing = False
            _atv_ix_prev_idle = True
            _reset_clock_saver_device_signal_baseline()
            _sync_status_bar_visibility_for_playback(None)
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def set_current_receiver_only(row: dict[str, str], *, persist: bool = True) -> None:
            """Persist AVR / AirPlay-only row for Denon HTTP overlay only; does not change Apple TV playback."""
            adr = str(row.get("address") or "").strip()
            if not adr:
                return
            if persist:
                write_last_receiver(
                    host=adr,
                    name=str(row.get("name") or "").strip() or None,
                    label=str(row.get("label") or "").strip() or None,
                    device_id=str(row.get("identifier") or "").strip() or None,
                )
            receiver_http_host["host"] = adr
            if playback_overlay_widget is not None:
                playback_overlay_widget.clear_cache()
            try:
                _warm_playback_overlay_blits()
            except Exception:
                pass
            nonlocal skip_cache
            skip_cache = None
            try:
                render_once()
            except Exception:
                pass
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def _apply_persisted_location_to_runtime() -> None:
            """Reload holders and runtime targets from the persisted current location."""
            nonlocal last_atv_interaction_mono, _atv_ix_sig_ds, _atv_ix_sig_ck
            nonlocal _atv_ix_pos, _atv_ix_pos_mono, _atv_ix_extrap_playing, _atv_ix_prev_idle
            nonlocal skip_cache
            streaming_slot_holder[0] = read_saved_streaming_device()
            avr_slot_holder[0] = read_saved_av_receiver()
            st2 = streaming_slot_holder[0]
            av2 = avr_slot_holder[0]
            if st2:
                current_apple_tv.clear()
                current_apple_tv.update(
                    {
                        "identifier": st2.get("identifier", ""),
                        "address": st2.get("address", ""),
                        "name": st2.get("name", ""),
                        "label": st2.get("label", ""),
                    }
                )
                write_last_apple_tv(
                    identifier=st2.get("identifier", ""),
                    address=st2.get("address", ""),
                    name=st2.get("name"),
                    label=st2.get("label"),
                )
            else:
                clear_last_apple_tv()
                current_apple_tv.clear()
                current_apple_tv.update({"identifier": "", "address": "", "name": "", "label": ""})
            apple_tv_auto_state["content_key"] = None
            apple_tv_auto_state["tmdb_key"] = None
            apple_tv_auto_state["query"] = None
            apple_tv_auto_state["last_metadata"] = None
            apple_tv_auto_state["last_tmdb_fetch_input"] = None
            apple_tv_auto_state["last_tmdb_fetch_refined"] = None
            apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
            apple_tv_playback_clock.clear()
            apple_tv_playback_clock.update(
                {
                    "has_sync": False,
                    "sync_mono": 0.0,
                    "sync_position": 0.0,
                    "live_mode": False,
                    "playing": False,
                    "latched_total": None,
                    "latched_content_key": None,
                    "last_reported_total": None,
                    "display_played_sec": None,
                    "trt_next_fire_mono": None,
                }
            )
            apple_tv_dashboard_track["last_poll_ok"] = None
            apple_tv_dashboard_track["consecutive_fail"] = 0
            last_atv_interaction_mono = 0.0
            _atv_ix_sig_ds = ""
            _atv_ix_sig_ck = None
            _atv_ix_pos = None
            _atv_ix_pos_mono = time.monotonic()
            _atv_ix_extrap_playing = False
            _atv_ix_prev_idle = True
            _reset_clock_saver_device_signal_baseline()
            if av2:
                adr = str(av2.get("address") or "").strip()
                if adr:
                    write_last_receiver(
                        host=adr,
                        name=str(av2.get("name") or "").strip() or None,
                        label=str(av2.get("label") or "").strip() or None,
                        device_id=str(av2.get("identifier") or "").strip() or None,
                    )
                    receiver_http_host["host"] = adr
            else:
                clear_last_receiver()
                receiver_http_host["host"] = ""
            if playback_overlay_widget is not None:
                playback_overlay_widget.clear_cache()
            try:
                _warm_playback_overlay_blits()
            except Exception:
                pass
            skip_cache = None
            _start_location_toast()
            _sync_status_bar_visibility_for_playback(None)
            try:
                render_once()
            except Exception:
                pass
            describe_current_apple_tv()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()

        def _refresh_location_selector() -> None:
            for w in location_om_frame.winfo_children():
                try:
                    w.destroy()
                except tk.TclError:
                    pass
            locs = read_all_locations_v2()
            labels: list[str] = []
            ids: list[str] = []
            counts: dict[str, int] = {}
            for L in locs:
                base = str(L.get("name") or "Room").strip() or "Room"
                counts[base] = counts.get(base, 0) + 1
                c = counts[base]
                lab = base if c == 1 else f"{base} ({c})"
                labels.append(lab)
                ids.append(str(L.get("id") or ""))
            labels.append("+ Custom location…")
            ids.append("__custom__")
            cur_id = read_current_location_id()

            def _pick(label_val: str) -> None:
                if label_val == "+ Custom location…":
                    name = simpledialog.askstring(
                        "Custom location",
                        "Name for this location:",
                        parent=root,
                    )
                    if name and str(name).strip():
                        add_empty_location_v2(str(name).strip())
                        _apply_persisted_location_to_runtime()
                    _refresh_location_selector()
                    return
                idx = labels.index(label_val) if label_val in labels else -1
                if idx < 0 or idx >= len(ids):
                    return
                lid = ids[idx]
                if lid == "__custom__":
                    return
                if lid == read_current_location_id():
                    return
                if lid and set_current_location_id(lid):
                    _apply_persisted_location_to_runtime()
                    _refresh_location_selector()

            if not locs:
                location_menu_var.set("+ Custom location…")

                def _pick_empty(val: str) -> None:
                    if val == "+ Custom location…":
                        name = simpledialog.askstring(
                            "Custom location",
                            "Name for this location:",
                            parent=root,
                        )
                        if name and str(name).strip():
                            add_empty_location_v2(str(name).strip())
                            _apply_persisted_location_to_runtime()
                        _refresh_location_selector()

                om = tk.OptionMenu(
                    location_om_frame,
                    location_menu_var,
                    "+ Custom location…",
                    command=_pick_empty,
                )
                om.pack(side=tk.LEFT)
                location_option_holder[0] = om
                location_name_var.set("")
                try:
                    location_name_entry.configure(state=tk.DISABLED)
                    rename_name_btn.configure(state=tk.DISABLED)
                    delete_location_btn.configure(state=tk.DISABLED)
                except tk.TclError:
                    pass
                return

            cur_label = labels[0]
            if cur_id:
                for i, xid in enumerate(ids):
                    if xid == cur_id and i < len(labels) - 1:
                        cur_label = labels[i]
                        break
            location_menu_var.set(cur_label)
            om = tk.OptionMenu(location_om_frame, location_menu_var, *labels, command=_pick)
            om.pack(side=tk.LEFT)
            location_option_holder[0] = om
            cid_nm = (read_current_location_id() or "").strip()
            raw_nm = ""
            if cid_nm:
                for L in read_all_locations_v2():
                    if str(L.get("id") or "").strip() == cid_nm:
                        raw_nm = str(L.get("name") or "Room").strip() or "Room"
                        break
            location_name_var.set(raw_nm)
            try:
                location_name_entry.configure(state=tk.NORMAL if cid_nm else tk.DISABLED)
                rename_name_btn.configure(state=tk.NORMAL if cid_nm else tk.DISABLED)
                delete_location_btn.configure(
                    state=tk.NORMAL if len(locs) > 1 and cid_nm else tk.DISABLED
                )
            except tk.TclError:
                pass

        def _on_delete_current_location() -> None:
            locs = read_all_locations_v2()
            if len(locs) <= 1:
                messagebox.showinfo(
                    "Delete location",
                    "You cannot delete the only location. Add another room first, then remove this one.",
                    parent=root,
                )
                return
            cid = (read_current_location_id() or "").strip()
            if not cid:
                return
            nm = "this location"
            for L in locs:
                if str(L.get("id") or "").strip() == cid:
                    nm = str(L.get("name") or "Room").strip() or "Room"
                    break
            if not messagebox.askyesno(
                "Delete location",
                f'Delete "{nm}" and remove all saved devices and Advanced (delegation) data '
                f"for that room?\n\nThis cannot be undone.",
                parent=root,
            ):
                return
            try:
                from pigeon.observed_capability import clear_observed_capabilities_for_location

                if not delete_location_v2(cid):
                    messagebox.showerror(
                        "Delete location",
                        "Could not delete that location.",
                        parent=root,
                    )
                    return
                clear_observed_capabilities_for_location(cid)
            except Exception as e:
                messagebox.showerror("Delete location", str(e), parent=root)
                return
            _apply_persisted_location_to_runtime()
            _refresh_location_selector()
            _start_location_toast()
            messagebox.showinfo("Delete location", f'"{nm}" was deleted.')

        delete_location_btn.configure(command=_on_delete_current_location)

        def _refresh_observed_pairing_led_rows() -> None:
            if not paired_observed_led_rows:
                return
            try:
                from device_capability_matrix import device_row_stable_key
            except Exception:
                for cv, _lid, _row in paired_observed_led_rows:
                    try:
                        _paint_cred_led_canvas(cv, False)
                    except tk.TclError:
                        pass
                return
            try:
                app_state = read_app_state()
            except Exception:
                app_state = {}
            observed_store_by_loc: dict[str, dict[str, object]] = {}
            for cv, lid, row in paired_observed_led_rows:
                try:
                    tri_state: bool | None = False
                    lid_s = str(lid or "").strip()
                    if lid_s:
                        if lid_s not in observed_store_by_loc:
                            raw_blob = app_state.get(f"observed_capability_live_v1.{lid_s}")
                            observed_store_by_loc[lid_s] = raw_blob if isinstance(raw_blob, dict) else {}
                        blob = observed_store_by_loc.get(lid_s, {})
                        sk = str(device_row_stable_key(row) or "").strip()
                        if sk:
                            feats = blob.get(sk) if isinstance(blob, dict) else None
                            if isinstance(feats, dict) and feats:
                                has_full = any(
                                    isinstance(v, str) and v == "full" for v in feats.values()
                                )
                                tri_state = True if has_full else None
                    k = id(cv)
                    if paired_observed_led_last_state.get(k, "__missing__") == tri_state:
                        continue
                    _paint_cred_led_canvas(cv, tri_state)
                    paired_observed_led_last_state[k] = tri_state
                except tk.TclError:
                    pass

        def _schedule_refresh_pairing_leds() -> None:

            stream_led = streaming_row_led_canvas_holder[0]
            # Extra pyatv scans here during discover/pair overlap the TV; refresh after busy clears instead.
            if apple_tv_busy["active"]:
                return
            if not _PIGEON_EXT:
                if stream_led is not None:
                    try:
                        _paint_boolean_led(stream_led, False)
                    except tk.TclError:
                        pass
                _paint_pair_led(0, False)
                _paint_pair_led(1, False)
                _refresh_observed_pairing_led_rows()
                return
            if pair_led_busy["active"]:
                if not _pair_led_pending_retry[0]:
                    _pair_led_pending_retry[0] = True

                    def _retry_pair_leds() -> None:
                        _pair_led_pending_retry[0] = False
                        _schedule_refresh_pairing_leds()

                    root.after(120, _retry_pair_leds)
                return
            pair_led_busy["active"] = True
            row_snap = streaming_slot_holder[0]

            def work() -> None:
                comp_sel, air_sel = False, False
                both_ok = False
                if row_snap:
                    try:
                        from pigeon.apple_tv_now_playing import apple_tv_pairing_credentials_status

                        c, a = apple_tv_pairing_credentials_status(
                            device_identifier=str(row_snap.get("identifier", "")),
                            device_address=str(row_snap.get("address", "")),
                        )
                        comp_sel, air_sel = bool(c), bool(a)
                        both_ok = comp_sel and air_sel
                    except Exception:
                        comp_sel, air_sel = False, False

                def apply_leds() -> None:
                    pair_led_busy["active"] = False
                    lpo = apple_tv_dashboard_track.get("last_poll_ok")
                    cf = int(apple_tv_dashboard_track.get("consecutive_fail", 0) or 0)
                    poll_unhealthy = lpo is False and cf >= 1

                    def _cred_led(has_cred: bool) -> bool | None:
                        if not has_cred:
                            return False
                        if poll_unhealthy:
                            return None
                        return True

                    stream_tri: bool | None = False
                    if row_snap and comp_sel and air_sel:
                        stream_tri = None if poll_unhealthy else True
                    elif row_snap and (comp_sel or air_sel):
                        stream_tri = False
                    if stream_led is not None:
                        try:
                            _paint_boolean_led(stream_led, stream_tri)
                        except tk.TclError:
                            pass
                    _paint_pair_led(0, _cred_led(comp_sel))
                    _paint_pair_led(1, _cred_led(air_sel))
                    pr = paired_ui_leds.get("remote")
                    pa = paired_ui_leds.get("airplay")
                    if pr is not None:
                        _paint_cred_led_canvas(pr, _cred_led(comp_sel))
                    if pa is not None:
                        _paint_cred_led_canvas(pa, _cred_led(air_sel))
                    _refresh_observed_pairing_led_rows()

                root.after(0, apply_leds)

            threading.Thread(target=work, daemon=True).start()

        def _device_addr_key(addr: str) -> str:
            s = str(addr or "").strip().lower()
            if not s:
                return ""
            if s.startswith("["):
                return s
            if s.count(":") == 1:
                left, right = s.rsplit(":", 1)
                if right.isdigit():
                    return left
            return s.split("%")[0]

        def _device_row_matches_saved(row: dict[str, str], saved: dict[str, str]) -> bool:
            ri = str(row.get("identifier") or "").strip()
            si = str(saved.get("identifier") or "").strip()
            if ri and si and ri == si:
                return True
            ra = _device_addr_key(str(row.get("address") or ""))
            sa = _device_addr_key(str(saved.get("address") or ""))
            return bool(ra and sa and ra == sa)

        def _verify_added_devices_after_save(added: list[dict[str, str]]) -> None:
            if not added:
                return

            def work() -> None:
                bad: list[str] = []
                for row in added:
                    addr = str(row.get("address") or "").strip()
                    if not addr:
                        continue
                    try:
                        from pigeon.apple_tv_now_playing import probe_pyatv_host

                        ok_w, msg_w, _, _ = probe_pyatv_host(addr, scan_timeout_s=6)
                    except Exception as e:
                        ok_w, msg_w = False, str(e)
                    if not ok_w:
                        label = str(row.get("label") or row.get("name") or addr)
                        tail = (msg_w or "no response")[:160]
                        bad.append(f"• {label} ({addr}): {tail}")

                def done() -> None:
                    if bad:
                        messagebox.showwarning(
                            "Device check",
                            "After saving, a quick follow-up scan could not reach some new entries "
                            "(sleeping, offline, or firewalled):\n\n" + "\n".join(bad),
                            parent=root,
                        )

                root.after(0, done)

            threading.Thread(target=work, daemon=True).start()

        describe_current_apple_tv()
        _refresh_location_selector()
        _rebuild_paired_devices_panel()

        def _ask_pairing_pin_modal(
            parent: tk.Misc,
            *,
            title: str,
            device_name: str,
            pair_kind: str,
            session_key: str | None,
        ) -> str | None:
            out: list[str | None] = [None]
            closed = [False]
            dlg = tk.Toplevel(parent)
            dlg.title(title)
            dlg.configure(bg="#1a1a1e")
            try:
                dlg.transient(root)
                dlg.grab_set()
            except tk.TclError:
                pass
            tk.Label(
                dlg,
                text=f"{pair_kind}\nDevice: {device_name}\nEnter the 4-digit code shown on the television.",
                fg="#ddd",
                bg="#1a1a1e",
                font=S_FONT_BODY,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, padx=14, pady=(12, 8))
            pin_var = tk.StringVar(value="")

            def _close() -> None:
                if closed[0]:
                    return
                closed[0] = True
                try:
                    dlg.grab_release()
                except tk.TclError:
                    pass
                dlg.destroy()

            ent = tk.Entry(
                dlg,
                textvariable=pin_var,
                width=12,
                font=("Menlo", 18) if sys.platform == "darwin" else ("Consolas", 18),
                justify=tk.CENTER,
                bg="#252528",
                fg="#e8e8e8",
                insertbackground="#e8e8e8",
            )
            ent.pack(padx=14, pady=(0, 8))

            def on_pin_write(*_args: object) -> None:
                raw = pin_var.get()
                d = "".join(c for c in raw if c.isdigit())[:4]
                if raw != d:
                    pin_var.set(d)
                    return
                if len(d) == 4 and not closed[0]:
                    out[0] = d
                    _close()

            pin_var.trace_add("write", on_pin_write)

            def append_digit(ch: str) -> None:
                cur = "".join(c for c in pin_var.get() if c.isdigit())
                if len(cur) < 4:
                    pin_var.set(cur + ch)

            def backspace() -> None:
                cur = "".join(c for c in pin_var.get() if c.isdigit())
                pin_var.set(cur[:-1])

            pad = tk.Frame(dlg, bg="#1a1a1e")
            pad.pack(padx=10, pady=(0, 10))
            for keys in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")):
                rf = tk.Frame(pad, bg="#1a1a1e")
                rf.pack()
                for d in keys:
                    tk.Button(
                        rf,
                        text=d,
                        width=4,
                        command=lambda x=d: append_digit(x),
                        font=S_FONT_BTN,
                    ).pack(side=tk.LEFT, padx=4, pady=4)
            rowz = tk.Frame(pad, bg="#1a1a1e")
            rowz.pack()
            tk.Button(rowz, text="\u232b", width=4, command=backspace, font=S_FONT_BTN).pack(
                side=tk.LEFT, padx=4, pady=4
            )
            tk.Button(rowz, text="0", width=4, command=lambda: append_digit("0"), font=S_FONT_BTN).pack(
                side=tk.LEFT, padx=4, pady=4
            )

            bf = tk.Frame(dlg, bg="#1a1a1e")
            bf.pack(pady=(0, 14))

            def on_ok() -> None:
                d = "".join(c for c in pin_var.get() if c.isdigit())
                if len(d) != 4:
                    messagebox.showwarning("Pairing", "Enter the 4-digit code from the TV.", parent=dlg)
                    return
                out[0] = d
                _close()

            def on_cancel() -> None:
                if session_key:
                    try:
                        from pigeon.apple_tv_now_playing import abandon_pairing_session

                        abandon_pairing_session(session_key)
                    except Exception:
                        pass
                out[0] = None
                _close()

            tk.Button(bf, text="OK", command=on_ok, font=S_FONT_BTN, padx=12, pady=4).pack(
                side=tk.LEFT, padx=8
            )
            tk.Button(bf, text="Cancel", command=on_cancel, font=S_FONT_BTN, padx=12, pady=4).pack(
                side=tk.LEFT, padx=8
            )
            dlg.protocol("WM_DELETE_WINDOW", on_cancel)
            ent.focus_set()
            dlg.wait_window()
            return out[0]

        def _finish_remote_then_start_airplay(row: dict[str, str], dn: str, session_key_w: str, pin: str) -> None:
            # Caller still holds apple_tv_busy from "starting AppleTV Remote" — do not poll until Remote is fully done.

            def worker_remote_finish() -> None:
                try:
                    from pigeon.apple_tv_now_playing import finish_companion_pairing_for_device

                    ok_f, msg_f = finish_companion_pairing_for_device(
                        session_key=session_key_w, pin_code=pin
                    )
                except ImportError:
                    ok_f, msg_f = False, _pyatv_install_hint()
                except Exception as e:
                    ok_f, msg_f = False, str(e)
                if ok_f:
                    time.sleep(1.5)

                def done_rf() -> None:
                    end_apple_tv_operation()
                    if not ok_f:
                        messagebox.showerror("AppleTV Remote", msg_f)
                        _schedule_refresh_pairing_leds()
                        return
                    messagebox.showinfo(
                        "AppleTV Remote",
                        f"{msg_f}\n\n"
                        "Wait until the Apple TV leaves the Remote pairing screen before continuing.",
                    )
                    _schedule_refresh_pairing_leds()
                    _start_airplay_pairing_sequence(row, dn)

                root.after(0, done_rf)

            threading.Thread(target=worker_remote_finish, daemon=True).start()

        def _start_airplay_pairing_sequence(row: dict[str, str], dn: str) -> None:
            if not messagebox.askokcancel(
                "AppleTV AirPlay",
                "Next: AppleTV AirPlay pairing will show a new code on the Apple TV.\n\n"
                "Continue only after the first (Remote) pairing has fully finished on the TV.\n\n"
                "Then open AirPlay / on-screen pairing on the Apple TV so it can show the next code.",
                parent=root,
            ):
                return
            if not begin_apple_tv_operation("starting AppleTV AirPlay"):
                return

            def worker_air_begin() -> None:
                try:
                    from pigeon.apple_tv_now_playing import begin_airplay_pairing_for_device

                    ok_a, msg_a, sk_a, _r2 = begin_airplay_pairing_for_device(
                        device_identifier=row["identifier"],
                        device_address=row["address"],
                        tv_displays_pin=True,
                    )
                except ImportError:
                    ok_a, msg_a, sk_a, _r2 = False, _pyatv_install_hint(), None, None
                except Exception as e:
                    ok_a, msg_a, sk_a, _r2 = False, str(e), None, None

                def ui_air_b() -> None:
                    if not ok_a or not sk_a:
                        end_apple_tv_operation()
                        messagebox.showerror("AppleTV AirPlay", msg_a or "Pairing failed to start.")
                        return
                    # Keep apple_tv_busy True until PIN is submitted so auto-poll cannot start a second connection.
                    describe_current_apple_tv(suffix="enter AirPlay PIN")
                    pin2 = _ask_pairing_pin_modal(
                        root,
                        title="AppleTV AirPlay",
                        device_name=dn,
                        pair_kind="AppleTV AirPlay pairing",
                        session_key=sk_a,
                    )
                    if pin2 is None:
                        end_apple_tv_operation()
                        messagebox.showinfo("AppleTV AirPlay", "Pairing cancelled.")
                        _schedule_refresh_pairing_leds()
                        return

                    def worker_air_finish() -> None:
                        try:
                            from pigeon.apple_tv_now_playing import finish_companion_pairing_for_device

                            ok_af, msg_af = finish_companion_pairing_for_device(
                                session_key=sk_a, pin_code=pin2
                            )
                        except ImportError:
                            ok_af, msg_af = False, _pyatv_install_hint()
                        except Exception as e:
                            ok_af, msg_af = False, str(e)

                        def done_af() -> None:
                            end_apple_tv_operation()
                            if ok_af:
                                messagebox.showinfo("AppleTV AirPlay", msg_af)
                            else:
                                messagebox.showerror("AppleTV AirPlay", msg_af)
                            _schedule_refresh_pairing_leds()

                        root.after(0, done_af)

                    threading.Thread(target=worker_air_finish, daemon=True).start()

                root.after(0, ui_air_b)

            threading.Thread(target=worker_air_begin, daemon=True).start()

        def _run_sequential_player_pairing_wizard(row: dict[str, str]) -> None:
            if not row_is_playback_apple_tv(row):
                return
            dn = str(row.get("name") or row.get("label") or "Apple TV")
            if not messagebox.askokcancel(
                "AppleTV Remote",
                "Pair AppleTV Remote first, then AppleTV AirPlay. Codes appear on the Apple TV.\n\n"
                "On the Apple TV: Settings → Remotes and Devices → Remote App and Devices — keep it open until a code appears.\n\n"
                "Continue?",
                parent=root,
            ):
                return
            if not begin_apple_tv_operation("starting AppleTV Remote"):
                return

            def worker_remote_begin() -> None:
                try:
                    from pigeon.apple_tv_now_playing import begin_companion_pairing_for_device

                    ok_w, msg_w, session_key_w, _rev = begin_companion_pairing_for_device(
                        device_identifier=row["identifier"],
                        device_address=row["address"],
                        tv_displays_pin=True,
                    )
                except ImportError:
                    ok_w, msg_w, session_key_w, _rev = False, _pyatv_install_hint(), None, None
                except Exception as e:
                    ok_w, msg_w, session_key_w, _rev = False, str(e), None, None

                def finish_rb() -> None:
                    if not ok_w or not session_key_w:
                        end_apple_tv_operation()
                        messagebox.showerror("AppleTV Remote", msg_w or "Pairing failed to start.")
                        return
                    # Stay busy through PIN entry so background Apple TV polling cannot connect yet.
                    describe_current_apple_tv(suffix="enter Remote PIN")
                    pin = _ask_pairing_pin_modal(
                        root,
                        title="AppleTV Remote",
                        device_name=dn,
                        pair_kind="AppleTV Remote pairing",
                        session_key=session_key_w,
                    )
                    if pin is None:
                        end_apple_tv_operation()
                        messagebox.showinfo("AppleTV Remote", "Pairing cancelled.")
                        _schedule_refresh_pairing_leds()
                        return
                    _finish_remote_then_start_airplay(row, dn, session_key_w, pin)

                root.after(0, finish_rb)

            threading.Thread(target=worker_remote_begin, daemon=True).start()

        def _force_advanced_feature_try(feature_id: str) -> None:
            """Advanced matrix refresh: re-run the probe path relevant to this feature row."""
            try:
                if feature_id == "title":
                    on_apple_tv_selected_then_tmdb()
                else:
                    if feature_id == "volume":
                        _receiver_poll_tick()
                    _apple_tv_auto_poll_tick()
            except Exception:
                pass

        def _on_advanced_matrix_closed() -> None:
            nonlocal dev_phase, skip_cache
            tgt = advanced_matrix_restore_phase[0]
            if tgt is not None:
                advanced_matrix_restore_phase[0] = None
                dev_phase = tgt  # type: ignore[assignment]
                skip_cache = None
                sync_developer_chrome()

        def _open_advanced_capability_matrix() -> None:
            nonlocal dev_phase, skip_cache
            try:
                from settings_advanced_matrix import open_advanced_capability_matrix
            except ImportError as e:
                messagebox.showerror("Advanced", f"Could not open capability matrix:\n{e}", parent=root)
                return
            if dev_phase == DevPhase.SETTINGS:
                advanced_matrix_restore_phase[0] = DevPhase.SETTINGS
                dev_phase = DevPhase.GRID
                skip_cache = None
                sync_developer_chrome()
            adv_kw: dict[str, object] = {
                "playback_content_ok": _advanced_feature_pipeline_ok,
                "on_closed": _on_advanced_matrix_closed,
                "close_skip_once": advanced_matrix_close_skip,
                "feature_force_try": _force_advanced_feature_try,
            }
            if _PIGEON_EXT:
                adv_kw.update(
                    {
                        "tmdb_manual_fetch": on_apple_tv_selected_then_tmdb,
                        "tmdb_report_failure": _perform_tmdb_artwork_retry,
                        "tmdb_read_log_tail": _tmdb_retry_log_read_tail,
                        "tmdb_register_widgets": _register_tmdb_adv_widgets,
                        "tmdb_unregister_widgets": _unregister_tmdb_adv_widgets,
                        "prepend_hotkey_bindtag": _prepend_hotkey_bindtag,
                        "tmdb_quality_stats_read": lambda: {
                            "successes": int(
                                read_app_state().get("tmdb_quality_successes", 0) or 0
                            ),
                            "failures": int(
                                read_app_state().get("tmdb_quality_failures", 0) or 0
                            ),
                        },
                    }
                )
            open_advanced_capability_matrix(root, **adv_kw)

        def _open_find_device_dialog() -> None:
            if not _PIGEON_EXT:
                messagebox.showinfo("Devices", "Pigeon extensions not loaded.")
                return
            top = tk.Toplevel(root)
            top.title("Find device")
            top.configure(bg="#1a1a1e")
            try:
                top.transient(root)
                top.grab_set()
            except tk.TclError:
                pass

            scan_rows: list[list[dict[str, str]]] = [[]]
            busy = {"v": False}
            confirm_holder: list[tk.Button | None] = [None]

            hdr = tk.Frame(top, bg="#1a1a1e")
            hdr.pack(fill=tk.X, padx=12, pady=(12, 8))
            find_btn = tk.Button(hdr, text="Find devices", font=S_FONT_BTN, padx=12, pady=4)
            refresh_btn = tk.Button(hdr, text="Refresh (network scan)", font=S_FONT_BTN, padx=10, pady=4)
            find_btn.pack(side=tk.LEFT, padx=(0, 8))
            refresh_btn.pack(side=tk.LEFT, padx=(0, 0))

            tk.Label(
                top,
                text="Use Find devices (cached scan when available) or Refresh for a live network scan. "
                "The list shows every device the scan returns (nothing is hidden). "
                "Pick a row or enter Host/IP, then Confirm — you will choose the device type and optional nickname. "
                "The same device can be saved more than once for different roles.",
                fg="#888",
                bg="#1a1a1e",
                font=S_FONT_MICRO,
                wraplength=560,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, padx=12, pady=(0, 6))

            search_banner_var = tk.StringVar(value="")
            tk.Label(
                top,
                textvariable=search_banner_var,
                fg="#ffb020",
                bg="#1a1a1e",
                font=("Helvetica", 22, "bold"),
            ).pack(anchor=tk.W, padx=12, pady=(0, 4))

            status_var = tk.StringVar(value="")

            loc_pick_var = tk.StringVar(value="")
            loc_pick_holder: list[list[tuple[str, str | None, str | None]]] = [[]]

            def build_location_pick_choices() -> list[tuple[str, str | None, str | None]]:
                ch: list[tuple[str, str | None, str | None]] = []
                counts: dict[str, int] = {}
                for L in read_all_locations_v2():
                    base = str(L.get("name") or "Room").strip() or "Room"
                    counts[base] = counts.get(base, 0) + 1
                    c = counts[base]
                    lab = base if c == 1 else f"{base} ({c})"
                    lid_g = str(L.get("id") or "").strip() or None
                    ch.append((lab, lid_g, None))
                for p in LOCATION_PRESET_ROOM_NAMES:
                    ch.append((f"+ New: {p}", None, p))
                ch.append(("+ New: Custom…", None, "__custom__"))
                return ch

            loc_pick_row = tk.Frame(top, bg="#1a1a1e")
            loc_pick_frame = tk.Frame(loc_pick_row, bg="#1a1a1e")

            def refresh_location_pick_menu() -> None:
                for w in loc_pick_frame.winfo_children():
                    try:
                        w.destroy()
                    except tk.TclError:
                        pass
                chs = build_location_pick_choices()
                loc_pick_holder[0] = chs
                labels = [t[0] for t in chs]
                cur = read_current_location_id()
                pick_default = labels[0] if labels else ""
                for disp, lid_g, _nn in chs:
                    if lid_g and lid_g == cur:
                        pick_default = disp
                        break
                if pick_default:
                    loc_pick_var.set(pick_default)
                if labels:
                    tk.OptionMenu(loc_pick_frame, loc_pick_var, *labels).pack(side=tk.LEFT)

            tk.Label(
                loc_pick_row,
                text="Save to location:",
                fg="#aaa",
                bg="#1a1a1e",
                font=S_FONT_SMALL,
            ).pack(side=tk.LEFT)
            loc_pick_frame.pack(side=tk.LEFT, padx=(8, 0))
            loc_pick_row.pack(anchor=tk.W, padx=12, pady=(0, 6))
            refresh_location_pick_menu()

            def resolve_save_location() -> tuple[str | None, str | None]:
                pick = str(loc_pick_var.get() or "")
                for disp, lid_g, nn in loc_pick_holder[0]:
                    if disp != pick:
                        continue
                    if nn == "__custom__":
                        name = simpledialog.askstring(
                            "Location name",
                            "Custom room name:",
                            parent=top,
                        )
                        return (None, (name or "").strip() or "Room")
                    if lid_g:
                        return (lid_g, None)
                    if nn:
                        return (None, nn)
                cur = read_current_location_id()
                return (cur or None, None)

            list_rows_holder: list[list[dict[str, str]]] = [[]]
            lb_frame = tk.Frame(top, bg="#1a1a1e")
            lb_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
            sb = tk.Scrollbar(lb_frame, orient=tk.VERTICAL)
            lb = tk.Listbox(
                lb_frame,
                height=12,
                bg=_LISTBOX_BG,
                fg=_LISTBOX_FG,
                font=S_FONT_STATUS,
                selectmode=tk.SINGLE,
                highlightthickness=1,
                highlightbackground="#333",
            )
            sb.config(command=lb.yview)
            lb.configure(yscrollcommand=sb.set)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            host_var = tk.StringVar(value="")

            def apply_listbox_rows(rows: list[dict[str, str]], *, empty_message: str | None = None) -> None:
                lb.delete(0, tk.END)
                list_rows_holder[0] = [dict(r) for r in rows]
                if not list_rows_holder[0]:
                    lb.insert(tk.END, empty_message or "No devices in list — try Refresh or Host / IP.")
                else:
                    for r in list_rows_holder[0]:
                        lb.insert(tk.END, str(r.get("label") or r.get("name") or r.get("address")))

            def repopulate_from_scan() -> None:
                rows_full = [dict(r) for r in scan_rows[0]]
                apply_listbox_rows(rows_full)

            def list_selection_row() -> dict[str, str] | None:
                sel = lb.curselection()
                if not sel:
                    return None
                idx = int(sel[0])
                rows_now = list_rows_holder[0]
                if idx < 0 or idx >= len(rows_now):
                    return None
                return dict(rows_now[idx])

            def set_scan(rows_in: list[dict[str, str]], msg: str) -> None:
                scan_rows[0] = [dict(r) for r in rows_in]
                status_var.set(msg)
                repopulate_from_scan()

            def run_scan(*, force_network: bool) -> None:
                if busy["v"]:
                    return
                busy["v"] = True
                now_chk = time.monotonic()
                cached_chk = discovery_scan_cache.get("rows")
                cache_mono_chk = float(discovery_scan_cache.get("mono_s") or 0.0)
                has_cache = (
                    not force_network
                    and isinstance(cached_chk, list)
                    and len(cached_chk) > 0
                    and (now_chk - cache_mono_chk) <= DISCOVERY_CACHE_TTL_S
                )
                search_banner_var.set("" if has_cache else "SEARCHING")
                status_var.set("Scanning\u2026" if force_network else "Loading\u2026")
                apply_listbox_rows(
                    [],
                    empty_message=(
                        "SEARCHING \u2014 scanning the network\u2026"
                        if not has_cache
                        else "Loading cached devices\u2026"
                    ),
                )
                for w in (find_btn, refresh_btn):
                    w.configure(state=tk.DISABLED)
                c = confirm_holder[0]
                if c is not None:
                    c.configure(state=tk.DISABLED)
                result: dict[str, object] = {}

                def worker() -> None:
                    now_m = time.monotonic()
                    rows_w: list[dict[str, str]] = []
                    ok_w = True
                    msg_w = ""
                    used_cache = False
                    cached = discovery_scan_cache.get("rows")
                    cache_mono = float(discovery_scan_cache.get("mono_s") or 0.0)
                    if (
                        not force_network
                        and isinstance(cached, list)
                        and len(cached) > 0
                        and (now_m - cache_mono) <= DISCOVERY_CACHE_TTL_S
                    ):
                        rows_w = [dict(r) for r in cached]
                        used_cache = True
                    else:
                        try:
                            from pigeon.apple_tv_now_playing import scan_apple_tv_devices

                            ok_w, msg_w, rows_w = scan_apple_tv_devices(scan_timeout_s=15)
                        except ImportError:
                            ok_w, msg_w, rows_w = False, _pyatv_install_hint(), []
                        except Exception as e:
                            ok_w, msg_w, rows_w = False, str(e), []
                        if ok_w and rows_w:
                            discovery_scan_cache["rows"] = [dict(r) for r in rows_w]
                            discovery_scan_cache["mono_s"] = time.monotonic()
                    result["ok"] = ok_w
                    result["rows"] = rows_w
                    result["msg"] = msg_w
                    result["used"] = used_cache

                def finish_scan() -> None:
                    busy["v"] = False
                    search_banner_var.set("")
                    for w in (find_btn, refresh_btn):
                        w.configure(state=tk.NORMAL)
                    c2 = confirm_holder[0]
                    if c2 is not None:
                        c2.configure(state=tk.NORMAL)
                    ok_w = bool(result.get("ok", True))
                    rows_w = result.get("rows") or []
                    msg_w = str(result.get("msg") or "")
                    used_cache = bool(result.get("used"))
                    if not isinstance(rows_w, list):
                        rows_w = []
                    if not ok_w:
                        messagebox.showerror("Find device", msg_w)
                        status_var.set("Scan failed.")
                        apply_listbox_rows([], empty_message="Search failed — try Refresh.")
                        return
                    if not rows_w:
                        messagebox.showinfo("Find device", msg_w or "No devices found.")
                        status_var.set("No devices.")
                        scan_rows[0] = []
                        apply_listbox_rows([], empty_message="No devices found — try Refresh or Host / IP.")
                        return
                    suffix = f"{len(rows_w)} found" + (" (cached)" if used_cache else "")
                    set_scan(rows_w, suffix)

                threading.Thread(target=lambda: (worker(), root.after(0, finish_scan)), daemon=True).start()

            def on_find_devices_click() -> None:
                run_scan(force_network=False)

            def on_refresh_click() -> None:
                run_scan(force_network=True)

            find_btn.configure(command=on_find_devices_click)
            refresh_btn.configure(command=on_refresh_click)

            tk.Label(
                top,
                text="Host / IP (optional, instead of list):",
                fg="#aaa",
                bg="#1a1a1e",
                font=S_FONT_SMALL,
            ).pack(anchor=tk.W, padx=12)
            tk.Entry(
                top,
                textvariable=host_var,
                width=36,
                bg="#252528",
                fg="#e8e8e8",
                insertbackground="#e8e8e8",
                highlightthickness=1,
                highlightbackground="#333",
                font=S_FONT_BODY,
            ).pack(anchor=tk.W, padx=12, pady=(2, 8))

            tk.Label(
                top,
                textvariable=status_var,
                fg="#777",
                bg="#1a1a1e",
                font=S_FONT_MICRO,
                wraplength=500,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, padx=12, pady=(0, 6))

            btn_row = tk.Frame(top, bg="#1a1a1e")
            btn_row.pack(pady=(0, 14))

            def close_top() -> None:
                try:
                    top.grab_release()
                except tk.TclError:
                    pass
                top.destroy()

            def on_cancel() -> None:
                close_top()

            def _after_find_device_save(lid_written: str, verify_rows: list[dict[str, str]]) -> None:
                if lid_written:
                    set_current_location_id(lid_written)
                _apply_persisted_location_to_runtime()
                _refresh_location_selector()
                _verify_added_devices_after_save(verify_rows)

            def _ask_save_device_role() -> str | None:
                choice: list[str | None] = [None]
                dlg = tk.Toplevel(top)
                dlg.title("Device type")
                dlg.configure(bg="#1a1a1e")
                try:
                    dlg.transient(top)
                    dlg.grab_set()
                except tk.TclError:
                    pass
                tk.Label(
                    dlg,
                    text="What kind of device is this?",
                    fg="#eee",
                    bg="#1a1a1e",
                    font=S_FONT_SMALL,
                ).pack(anchor=tk.W, padx=12, pady=(12, 8))
                row_f = tk.Frame(dlg, bg="#1a1a1e")
                row_f.pack(fill=tk.X, padx=12, pady=(0, 8))
                var = tk.StringVar(value="player")
                for lab, val in (
                    ("Player (playback / metadata)", "player"),
                    ("Receiver (Denon/Marantz-style IP)", "receiver"),
                    ("TV", "tv"),
                    ("Projector", "projector"),
                    ("Game console", "game"),
                    ("Other", "other"),
                ):
                    tk.Radiobutton(
                        row_f,
                        text=lab,
                        variable=var,
                        value=val,
                        bg="#1a1a1e",
                        fg="#eee",
                        selectcolor="#333",
                        activebackground="#1a1a1e",
                        highlightthickness=0,
                        font=S_FONT_SMALL,
                    ).pack(anchor=tk.W)

                def ok() -> None:
                    choice[0] = str(var.get() or "").strip() or None
                    dlg.destroy()

                def cancel() -> None:
                    choice[0] = None
                    dlg.destroy()

                br = tk.Frame(dlg, bg="#1a1a1e")
                br.pack(pady=(0, 12))
                tk.Button(br, text="OK", command=ok, font=S_FONT_BTN, padx=14, pady=4).pack(
                    side=tk.LEFT, padx=6
                )
                tk.Button(br, text="Cancel", command=cancel, font=S_FONT_BTN, padx=14, pady=4).pack(
                    side=tk.LEFT, padx=6
                )
                dlg.wait_window(dlg)
                return choice[0]

            def on_confirm() -> None:
                host = str(host_var.get() or "").strip()

                def _tag_row_device_role(row: dict[str, str], dr: str) -> None:
                    row["device_role"] = dr

                base_row: dict[str, str] | None = None
                if not host:
                    base_row = list_selection_row()
                    if base_row is None:
                        messagebox.showwarning(
                            "Find device",
                            "Select a device from the list (wait until search finishes), or enter Host / IP.",
                            parent=top,
                        )
                        return

                r0 = _ask_save_device_role()
                if not r0:
                    return

                nick_raw = simpledialog.askstring(
                    "Nickname",
                    "Optional nickname for this entry (shown in lists and Advanced):",
                    parent=top,
                )
                nick = (nick_raw or "").strip()

                def _merge_nick(row: dict[str, str]) -> dict[str, str]:
                    m = dict(row)
                    if nick:
                        m["nickname"] = nick
                    return m

                to_id, new_nm = resolve_save_location()

                if r0 in ("tv", "projector", "game", "other"):
                    if host:
                        ident = f"{r0}:{host.split('%')[0].strip()}"
                        nm = r0.capitalize() if r0 != "other" else "Other"
                        row_any = {
                            "identifier": ident,
                            "address": host.strip(),
                            "name": nm,
                            "label": f"{nm} — {host.strip()}",
                            "looks_like_apple_tv": "false",
                        }
                        _tag_row_device_role(row_any, r0)
                    else:
                        row_any = _merge_nick(dict(base_row or {}))
                        _tag_row_device_role(row_any, r0)
                    row_any = _merge_nick(row_any)
                    slot_key = {"tv": "tv", "projector": "projector", "game": "game", "other": "other"}[r0]
                    lid = append_device_to_location_slot(
                        slot_key,
                        row_any,
                        for_location_id=to_id,
                        new_location_name=new_nm,
                    )
                    _after_find_device_save(lid, [row_any])
                    close_top()
                    return

                if r0 == "receiver":
                    if host:
                        row_r = {
                            "identifier": f"denon:{host.split('%')[0].strip()}",
                            "address": host.strip(),
                            "name": "Receiver",
                            "label": f"Receiver \u2014 {host.strip()}",
                            "looks_like_apple_tv": "false",
                        }
                        _tag_row_device_role(row_r, "receiver")
                    else:
                        row_r = _merge_nick(dict(base_row or {}))
                        _tag_row_device_role(row_r, "receiver")
                    row_r = _merge_nick(row_r)
                    lid = append_device_to_location_slot(
                        "av_receiver",
                        row_r,
                        for_location_id=to_id,
                        new_location_name=new_nm,
                    )
                    _after_find_device_save(lid, [row_r])
                    close_top()
                    return

                # Player
                if host:
                    close_top()
                    if not begin_apple_tv_operation("probing address"):
                        return

                    def w_probe() -> None:
                        try:
                            from pigeon.apple_tv_now_playing import probe_pyatv_host

                            ok_w, msg_w, row_w, looks_w = probe_pyatv_host(host, scan_timeout_s=8)
                        except ImportError:
                            ok_w, msg_w, row_w, looks_w = False, _pyatv_install_hint(), None, False
                        except Exception as e:
                            ok_w, msg_w, row_w, looks_w = False, str(e), None, False

                        def d_probe() -> None:
                            end_apple_tv_operation()
                            if not ok_w or row_w is None:
                                messagebox.showerror("Find device", msg_w)
                                return
                            row_d = _merge_nick(dict(row_w))
                            _tag_row_device_role(row_d, "player")
                            lid = append_device_to_location_slot(
                                "streaming",
                                row_d,
                                for_location_id=to_id,
                                new_location_name=new_nm,
                            )
                            _after_find_device_save(lid, [row_d])
                            if row_is_playback_apple_tv(row_d):
                                _run_sequential_player_pairing_wizard(row_d)

                        root.after(0, d_probe)

                    threading.Thread(target=w_probe, daemon=True).start()
                    return

                row_p = _merge_nick(dict(base_row or {}))
                _tag_row_device_role(row_p, "player")
                lid = append_device_to_location_slot(
                    "streaming",
                    row_p,
                    for_location_id=to_id,
                    new_location_name=new_nm,
                )
                _after_find_device_save(lid, [row_p])
                close_top()
                if row_is_playback_apple_tv(row_p):
                    _run_sequential_player_pairing_wizard(row_p)

            confirm_btn = tk.Button(btn_row, text="Confirm", command=on_confirm, font=S_FONT_BTN, padx=12, pady=4)
            confirm_holder[0] = confirm_btn
            cancel_btn = tk.Button(btn_row, text="Cancel", command=on_cancel, font=S_FONT_BTN, padx=12, pady=4)
            confirm_btn.pack(side=tk.LEFT, padx=8)
            cancel_btn.pack(side=tk.LEFT, padx=8)
            top.protocol("WM_DELETE_WINDOW", on_cancel)
            run_scan(force_network=False)

        def on_reset_pigeon_devices_and_media() -> None:
            nonlocal skip_cache
            if not messagebox.askokcancel(
                "Reset",
                "This wipes everything Pigeon has stored for devices and local image media:\n\n"
                "• Saved locations, Players, and Receivers\n"
                "• pyatv credentials\n"
                "• Discovery cache\n"
                "• pigeonPulledMedia and pigeonReformattedMedia\n\n"
                "This cannot be undone. Continue?",
                parent=root,
            ):
                return
            ok1, msg1 = purge_directory_contents(pigeon_pulled_media_dir())
            ok2, msg2 = purge_directory_contents(pigeon_reformatted_media_dir())
            cred_path = pigeon_state_dir() / "pyatv_credentials"
            cred_err = ""
            try:
                if cred_path.is_file():
                    cred_path.unlink()
            except OSError as e:
                cred_err = str(e)
            clear_all_persisted_devices_and_targets()
            clear_last_apple_tv()
            clear_last_receiver()
            discovery_scan_cache["rows"] = None
            discovery_scan_cache["mono_s"] = 0.0
            streaming_slot_holder[0] = None
            avr_slot_holder[0] = None
            current_apple_tv.clear()
            current_apple_tv.update(
                {"identifier": "", "address": "", "name": "", "label": ""}
            )
            receiver_http_host["host"] = ""
            apple_tv_auto_state["content_key"] = None
            apple_tv_auto_state["tmdb_key"] = None
            apple_tv_auto_state["query"] = None
            apple_tv_auto_state["last_metadata"] = None
            apple_tv_auto_state["last_tmdb_fetch_input"] = None
            apple_tv_auto_state["last_tmdb_fetch_refined"] = None
            apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
            apple_tv_dashboard_track["last_poll_ok"] = None
            apple_tv_dashboard_track["consecutive_fail"] = 0
            if playback_overlay_widget is not None:
                playback_overlay_widget.clear_cache()
            describe_current_apple_tv()
            _refresh_location_selector()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()
            try:
                _warm_playback_overlay_blits()
            except Exception:
                pass
            skip_cache = None
            try:
                render_once()
            except Exception:
                pass
            tail = f"{msg1}\n{msg2}"
            if cred_err:
                tail += f"\nCredentials file: {cred_err}"
            if ok1 and ok2 and not cred_err:
                messagebox.showinfo("Reset", tail)
            else:
                messagebox.showwarning("Reset", tail)

        def on_apple_tv_selected_then_tmdb() -> None:
            """Use the saved streaming slot: pyatv (Apple TV) or Roku ECP, then TMDb + backdrop."""
            if not _PIGEON_EXT:
                messagebox.showinfo("Devices", "Pigeon extensions not loaded.")
                return
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            row = streaming_slot_holder[0]
            if row is None:
                _open_find_device_dialog()
                return
            if not begin_apple_tv_operation("detecting content"):
                return

            def worker() -> None:
                ok_w, msg_w, title_w = False, "", None
                if row_is_playback_apple_tv(row):
                    try:
                        from pigeon.apple_tv_now_playing import fetch_now_playing_title_for_device

                        ok_w, msg_w, title_w = fetch_now_playing_title_for_device(
                            device_identifier=row["identifier"],
                            device_address=row["address"],
                        )
                    except ImportError:
                        ok_w, msg_w, title_w = (
                            False,
                            _pyatv_install_hint(),
                            None,
                        )
                    except Exception as e:
                        ok_w, msg_w, title_w = False, str(e), None
                else:
                    try:
                        from pigeon.roku_ecp import (
                            fetch_roku_title_for_metadata,
                            resolve_roku_ecp_base_url_for_row,
                        )

                        rbase = resolve_roku_ecp_base_url_for_row(row)
                        if not rbase:
                            ok_w, msg_w, title_w = (
                                False,
                                "",
                                None,
                            )
                        else:
                            ok_w, msg_w, title_w = fetch_roku_title_for_metadata(
                                rbase, timeout=10.0
                            )
                    except Exception as e:
                        ok_w, msg_w, title_w = False, str(e), None

                def finish() -> None:
                    nonlocal last_atv_interaction_mono
                    if not row_is_playback_apple_tv(row) and not ok_w and not msg_w:
                        end_apple_tv_operation()
                        messagebox.showinfo(
                            "Devices",
                            "This Player is not an Apple TV (pyatv) row, and Pigeon could not use "
                            "Roku ECP on its IP (port 8060).\n\n"
                            "• If this is a Roku / Roku TV (e.g. Onn), ensure the TV’s IP is in the "
                            "Player slot and try again, or set \"roku_ecp_base_url\" in "
                            f"{PIGEON_STATE_DIR_TILDE}/state.json to http://TV_IP:8060\n"
                            "• For an actual Apple TV, re-add it from Find devices so the label "
                            "shows “Apple TV / tvOS”.\n"
                            "• For a receiver only, choose Receiver in Find device for the overlay.",
                        )
                        return
                    if not ok_w:
                        end_apple_tv_operation()
                        messagebox.showerror("Devices", msg_w or "Could not read now playing.")
                        return
                    if not title_w:
                        end_apple_tv_operation()
                        messagebox.showinfo(
                            "Devices",
                            msg_w or "No title reported by the selected device.",
                        )
                        return
                    from pigeon.tmdb_poster import is_degenerate_tmdb_query

                    if is_degenerate_tmdb_query(title_w):
                        end_apple_tv_operation()
                        messagebox.showinfo(
                            "Devices",
                            "The device only reported app or channel branding, not the show or movie "
                            "title, so Pigeon did not search TMDb.\n\n"
                            "On Disney+ via Roku, wait until playback has started and try Manual fetch again.",
                        )
                        return
                    set_current_apple_tv(row, persist=True)
                    last_atv_interaction_mono = time.monotonic()
                    end_apple_tv_operation(suffix="title detected")
                    sys.stderr.write(f"pigeon: {msg_w}\n")
                    sys.stderr.flush()
                    spawn_tmdb_poster_fetch(title_w, prefer="auto")

                root.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        def _update_status_bar_from_metadata(metadata: dict[str, object] | None) -> None:
            if metadata:
                _apply_playback_clock_from_poll(metadata)
            # Sync TRT digits to the latest polled integer second. The steady 1 Hz metronome
            # continues stepping from this anchor.
            _sync_trt_text_to_true_once()

        def _content_key_from_metadata(metadata: dict[str, object]) -> str | None:
            query = str(metadata.get("query") or "").strip()
            if not query:
                return None
            media_type = str(metadata.get("media_type") or "")
            title = str(metadata.get("title") or "")
            series_name = str(metadata.get("series_name") or "")
            artist = str(metadata.get("artist") or "")
            total_time = str(metadata.get("total_time") or "")
            return "|".join((query, media_type, title, series_name, artist, total_time))

        def _tmdb_pref_from_metadata(metadata: dict[str, object]) -> str:
            prefer = str(metadata.get("prefer") or "auto").strip().lower()
            return prefer if prefer in ("auto", "tv", "movie") else "auto"

        def _apply_playback_clock_from_poll(metadata: dict[str, object]) -> None:
            """Anchor wall clock to last reported position; polls resync and correct drift."""
            nonlocal last_timecode_motion_mono
            clk = apple_tv_playback_clock
            ds = str(metadata.get("device_state") or "")
            playing_now = "Playing" in ds
            now_m = time.monotonic()
            content_key = _content_key_from_metadata(metadata)
            prev_has_sync = bool(clk.get("has_sync"))
            prev_pos = float(clk.get("sync_position") or 0.0)

            tt_raw = metadata.get("total_time")
            try:
                reported_total = float(tt_raw) if tt_raw is not None else None
            except (TypeError, ValueError):
                reported_total = None
            if reported_total is not None:
                clk["last_reported_total"] = reported_total

            # Live/continuous content: Apple TV often doesn't provide total_time/position.
            # Show LIVE instead of attempting to run the TRT extrapolator.
            live_now = bool(playing_now) and (reported_total is None or reported_total <= 0)
            if live_now:
                clk["live_mode"] = True
                clk["has_sync"] = False
                clk["latched_total"] = None
                clk["display_played_sec"] = None
                clk["trt_next_fire_mono"] = None
                clk["playing"] = True
                # Still latch content so TMDb artwork doesn't keep swapping.
                if content_key and content_key != clk.get("latched_content_key"):
                    clk["latched_content_key"] = content_key
                return
            clk["live_mode"] = False

            if content_key and content_key != clk.get("latched_content_key"):
                clk["latched_content_key"] = content_key
                clk["latched_total"] = reported_total
                clk["display_played_sec"] = None
                clk["trt_next_fire_mono"] = None

            pos_raw = metadata.get("position")
            pos_f: float | None = None
            if pos_raw is not None:
                try:
                    pos_f = max(0.0, float(pos_raw))
                except (TypeError, ValueError):
                    pos_f = None

            if pos_f is not None:
                clk["sync_mono"] = now_m
                clk["sync_position"] = pos_f
                clk["playing"] = playing_now
                clk["has_sync"] = True
                if playing_now and (not prev_has_sync or abs(pos_f - prev_pos) >= 0.25):
                    last_timecode_motion_mono = now_m
                return

            if not clk.get("has_sync"):
                clk["playing"] = playing_now
                return

            sp = float(clk["sync_position"])
            sm = float(clk["sync_mono"])
            extrap = sp + (now_m - sm) if clk.get("playing") else sp
            extrap = max(0.0, extrap)
            lt = clk.get("latched_total")
            if lt is not None:
                try:
                    extrap = min(extrap, float(lt))
                except (TypeError, ValueError):
                    pass
            clk["sync_position"] = extrap
            clk["sync_mono"] = now_m
            clk["playing"] = playing_now
            if playing_now and abs(extrap - sp) >= 0.25:
                last_timecode_motion_mono = now_m

        def _playback_extrapolated_pair() -> tuple[int, int] | None:
            clk = apple_tv_playback_clock
            if clk.get("live_mode"):
                return None
            if not clk.get("has_sync"):
                return None
            now_m = time.monotonic()
            sp = float(clk["sync_position"])
            sm = float(clk["sync_mono"])
            pos = sp + (now_m - sm) if clk.get("playing") else sp
            pos = max(0.0, pos)
            lt = clk.get("latched_total")
            if lt is None:
                lt = clk.get("last_reported_total")
            if lt is not None:
                try:
                    tft = float(lt)
                    pos = min(pos, tft)
                except (TypeError, ValueError):
                    tft = None
                else:
                    played = int(pos)
                    remaining = max(0, int(tft) - played)
                    return played, remaining
            return int(pos), 0

        def _refresh_trt_progress_only() -> None:
            nonlocal skip_cache
            if status_bar_widget is None:
                return
            pfrac = _playback_progress_fraction_for_bar()
            prog = pfrac if pfrac is not None else 0.0
            if status_bar_widget.set_now_playing_display(progress=prog):
                _warm_status_bar_blits()
                skip_cache = None

        def _sync_trt_text_to_true_once() -> None:
            """Set TRT text to the latest polled integer second (used to recover from missed metronome ticks)."""
            nonlocal skip_cache
            try:
                if status_bar_widget is None:
                    return
                clk = apple_tv_playback_clock
                if clk.get("live_mode"):
                    # Live: digits are suppressed; show LIVE.
                    if status_bar_widget.set_now_playing_display(
                        played_text="LIVE",
                        remaining_text="",
                        progress=0.0,
                    ):
                        _warm_status_bar_blits()
                        skip_cache = None
                    return

                pair = _playback_extrapolated_pair()
                if pair is None:
                    return
                played_true, rem_true = pair
                disp_true = int(played_true)

                # If we're already showing this second, avoid touching TRT text cadence.
                disp_cur = clk.get("display_played_sec")
                if disp_cur is not None and int(disp_cur) == disp_true:
                    _refresh_trt_progress_only()
                    return

                clk["display_played_sec"] = disp_true
                played_text = _format_hmmss(disp_true)
                remaining_text = _format_hmmss(int(rem_true))
                pfrac = _playback_progress_fraction_for_bar()
                prog = pfrac if pfrac is not None else 0.0
                if status_bar_widget.set_now_playing_display(
                    played_text=played_text,
                    remaining_text=remaining_text,
                    progress=prog,
                ):
                    _warm_status_bar_blits()
                    skip_cache = None
            finally:
                _sync_status_bar_trt_substantive()

        def _playback_progress_fraction_for_bar() -> float | None:
            """Integer-second played / total for progress bar (None if duration unknown)."""
            clk = apple_tv_playback_clock
            if clk.get("live_mode"):
                return None
            if not clk.get("has_sync"):
                return None
            lt = clk.get("latched_total")
            if lt is None:
                lt = clk.get("last_reported_total")
            if lt is None:
                return None
            try:
                total_f = float(lt)
            except (TypeError, ValueError):
                return None
            if total_f <= 0:
                return None
            now_m = time.monotonic()
            sp = float(clk["sync_position"])
            sm = float(clk["sync_mono"])
            pos = sp + (now_m - sm) if clk.get("playing") else sp
            pos = max(0.0, min(pos, total_f))
            played_i = int(pos)
            total_i = max(1, int(round(total_f)))
            return max(0.0, min(1.0, played_i / float(total_i)))

        def _refresh_extrapolated_timecodes(*, tick_steps: int = 1) -> None:
            """Update TRT labels + progress using stepped display time (steady rhythm)."""
            nonlocal skip_cache
            if status_bar_widget is None:
                return
            try:
                clk = apple_tv_playback_clock
                if clk.get("live_mode"):
                    if status_bar_widget.set_now_playing_display(
                        played_text="LIVE",
                        remaining_text="",
                        progress=0.0,
                    ):
                        _warm_status_bar_blits()
                        skip_cache = None
                    return
                pair = _playback_extrapolated_pair()
                pfrac = _playback_progress_fraction_for_bar()
                if pair is None:
                    clk["display_played_sec"] = None
                    clk["trt_next_fire_mono"] = None
                    if status_bar_widget.set_now_playing_display(progress=0.0):
                        _warm_status_bar_blits()
                        skip_cache = None
                    return
                played_true, _rem_true = pair
                lt_use = clk.get("latched_total")
                if lt_use is None:
                    lt_use = clk.get("last_reported_total")
                playing = bool(clk.get("playing"))
                disp = clk.get("display_played_sec")

                if disp is None:
                    disp = int(played_true)
                elif not playing:
                    disp = int(played_true)
                else:
                    pt = int(played_true)
                    diff = pt - disp
                    # Keep the on-screen cadence steady: at most ±1 per tick for small drift.
                    # Snap only for major seeks (e.g. user scrubs).
                    if diff >= 10:
                        disp = pt
                    elif diff > 0:
                        disp = min(pt, disp + 1)
                    elif diff <= -10:
                        disp = pt
                    elif diff < 0:
                        disp = max(pt, disp - 1)
                clk["display_played_sec"] = disp

                if lt_use is not None:
                    try:
                        tft = int(round(float(lt_use)))
                        remaining_secs = max(0, tft - disp)
                    except (TypeError, ValueError):
                        remaining_secs = max(0, int(_rem_true) + int(played_true) - disp)
                else:
                    remaining_secs = max(0, int(_rem_true) + int(played_true) - disp)

                played_text = _format_hmmss(disp)
                remaining_text = _format_hmmss(remaining_secs)
                prog = pfrac if pfrac is not None else 0.0
                if status_bar_widget.set_now_playing_display(
                    played_text=played_text,
                    remaining_text=remaining_text,
                    progress=prog,
                ):
                    _warm_status_bar_blits()
                    skip_cache = None
            finally:
                _sync_status_bar_trt_substantive()

        def _playback_ui_tick() -> None:
            try:
                clk = apple_tv_playback_clock
                now_m = time.monotonic()
                nf = clk.get("trt_next_fire_mono")
                if nf is None:
                    nf = now_m + 1.0
                    clk["trt_next_fire_mono"] = nf
                nf = float(nf)
                tick_steps = 0
                while nf <= now_m and tick_steps < 12:
                    nf += 1.0
                    tick_steps += 1
                if tick_steps == 0:
                    root.after(max(1, int(round((nf - now_m) * 1000))), _playback_ui_tick)
                    return
                clk["trt_next_fire_mono"] = nf
                _refresh_extrapolated_timecodes(tick_steps=tick_steps)
                _refresh_content_indicator()
                now2 = time.monotonic()
                next_nf_raw = clk.get("trt_next_fire_mono")
                if next_nf_raw is None:
                    next_nf_raw = now2 + 1.0
                    clk["trt_next_fire_mono"] = next_nf_raw
                next_nf = float(next_nf_raw)
                delay_ms = max(1, min(120_000, int(round((next_nf - now2) * 1000))))
                root.after(delay_ms, _playback_ui_tick)
            except Exception:
                # Never let TRT tick crashes take down the UI loop (discovery/pairing run on Tk too).
                try:
                    clk = apple_tv_playback_clock
                    clk["trt_next_fire_mono"] = time.monotonic() + 1.0
                except Exception:
                    pass
                root.after(1000, _playback_ui_tick)

        def _update_atv_interaction_from_poll_metadata(metadata: dict[str, object]) -> None:
            """Approximate Siri Remote / UI use from pyatv poll deltas (not plain playback time)."""
            nonlocal last_atv_interaction_mono, _atv_ix_sig_ds, _atv_ix_sig_ck
            nonlocal _atv_ix_pos, _atv_ix_pos_mono, _atv_ix_extrap_playing, _atv_ix_prev_idle
            if not current_apple_tv.get("identifier"):
                return
            now = time.monotonic()
            ds = str(metadata.get("device_state") or "")
            ck = _content_key_from_metadata(metadata)
            idle_now = _atv_metadata_is_content_idle(metadata)
            pos_raw = metadata.get("position")
            try:
                pos = float(pos_raw) if pos_raw is not None else None
            except (TypeError, ValueError):
                pos = None

            bump = False
            if _atv_ix_sig_ds and ds != _atv_ix_sig_ds:
                bump = True
            if ck != _atv_ix_sig_ck and (ck or _atv_ix_sig_ck):
                bump = True
            if not idle_now and _atv_ix_prev_idle:
                bump = True
            if (
                pos is not None
                and _atv_ix_pos is not None
                and 0 < (now - _atv_ix_pos_mono) < 60.0
            ):
                dt = now - _atv_ix_pos_mono
                expected = _atv_ix_pos + (dt if _atv_ix_extrap_playing else 0.0)
                if abs(pos - expected) > 3.0:
                    bump = True

            if bump:
                last_atv_interaction_mono = now
                last_device_interaction_mono = now

            _atv_ix_sig_ds = ds
            _atv_ix_sig_ck = ck
            _atv_ix_prev_idle = idle_now
            if pos is not None:
                _atv_ix_pos = pos
                _atv_ix_pos_mono = now
            _atv_ix_extrap_playing = "Playing" in ds

        def _trt_substantive_from_clock() -> bool:
            clk = apple_tv_playback_clock
            if clk.get("live_mode"):
                return False
            if not clk.get("has_sync"):
                return False
            lt = clk.get("latched_total")
            if lt is None:
                lt = clk.get("last_reported_total")
            if lt is None:
                return False
            try:
                return float(lt) > 0.0
            except (TypeError, ValueError):
                return False

        def _sync_status_bar_trt_substantive() -> None:
            nonlocal skip_cache
            if status_bar_widget is None:
                return
            if status_bar_widget.set_trt_substantive(_trt_substantive_from_clock()):
                _warm_status_bar_blits()
                skip_cache = None

        def _sync_status_bar_visibility_for_playback(metadata: dict[str, object] | None) -> None:
            """Hide now-playing bar + TRT pills when no Apple TV or idle (nothing queued); show when content is active."""
            nonlocal skip_cache
            if status_bar_widget is None:
                return
            if not current_apple_tv.get("identifier"):
                show = False
            elif metadata is not None:
                show = not _atv_metadata_is_content_idle(metadata)
            else:
                clk = apple_tv_playback_clock
                show = bool(clk.get("has_sync"))
            if status_bar_widget.set_now_playing_chrome_visible(show):
                _warm_status_bar_blits()
                skip_cache = None
            _sync_status_bar_trt_substantive()

        def _sync_streaming_badge_from_playback_sources(
            md: dict[str, object] | None,
            *,
            roku_app_name: str | None = None,
        ) -> None:
            """Streaming-service badge: pyatv metadata when present, else Roku ECP active-app name."""
            nonlocal skip_cache
            if playback_overlay_widget is None:
                return
            from pigeon.streaming_service_badges import resolve_streaming_badge_media

            assets = Path(_PROJECT_DIR) / "pigeonAssets"
            show = False
            filename = ""
            label = ""
            md_active = md is not None and not _atv_metadata_is_content_idle(md)
            pyatv_app = False
            if md_active:
                an = str(md.get("app_name") or "").strip()
                bid = str(md.get("app_id") or "").strip()
                pyatv_app = bool(an or bid)
                if pyatv_app:
                    fn, label = resolve_streaming_badge_media(
                        assets,
                        app_name=an,
                        app_id=bid,
                    )
                    if fn or label:
                        show = True
                        filename = fn or ""
            if not show and roku_app_name and str(roku_app_name).strip():
                ra = str(roku_app_name).strip()
                fn, label = resolve_streaming_badge_media(
                    assets,
                    app_name=ra,
                    app_id="",
                )
                if fn or label:
                    show = True
                    filename = fn or ""
            if not show and md_active:
                fn, label = resolve_streaming_badge_media(
                    assets,
                    app_name=str(md.get("app_name") or ""),
                    app_id=str(md.get("app_id") or ""),
                )
                if fn or label:
                    show = True
                    filename = fn or ""
            if not show and isinstance(md, dict):
                an = str(md.get("app_name") or "").strip()
                bid = str(md.get("app_id") or "").strip()
                if an or bid:
                    fn, label = resolve_streaming_badge_media(
                        assets,
                        app_name=an,
                        app_id=bid,
                    )
                    if fn or label:
                        show = True
                        filename = fn or ""
            streaming_badge_state["show"] = show
            streaming_badge_state["filename"] = filename
            streaming_badge_state["label"] = label
            _warm_playback_overlay_blits()
            skip_cache = None
            _apply_netflix_backdrop_when_running()
            try:
                render_once()
            except Exception:
                pass

        def _return_to_landing_if_atv_idle(metadata: dict[str, object]) -> None:
            """When Apple TV reports no playback, drop TMDb backdrop and show the static landing page."""
            nonlocal use_backdrop_scene, backdrop_master_bgr, backdrop_app_logo_letterbox_fit, last_frame, scaled_display, scaled_version, skip_cache
            nonlocal active_tmdb_title_key, active_tmdb_display_title, tmdb_logo_patch_bgra, scene_enabled, playing
            nonlocal tmdb_logo_app_fallback_active
            if not _atv_metadata_is_content_idle(metadata):
                return
            apple_tv_auto_state["content_key"] = None
            apple_tv_auto_state["query"] = None
            apple_tv_auto_state["prefer"] = "auto"
            apple_tv_auto_state["last_tmdb_fetch_input"] = None
            apple_tv_auto_state["last_tmdb_fetch_refined"] = None
            apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
            lm = apple_tv_auto_state.get("last_metadata")
            if isinstance(lm, dict):
                lm["query"] = ""
                lm["content_key"] = None

            had_art = bool(use_backdrop_scene or backdrop_master_bgr is not None or active_tmdb_title_key)

            use_backdrop_scene = False
            backdrop_master_bgr = None
            backdrop_app_logo_letterbox_fit = False
            playing = False
            active_tmdb_title_key = None
            active_tmdb_display_title = None
            tmdb_logo_app_fallback_active = False
            if tmdb_logo_widget is not None:
                tmdb_logo_widget.clear_cache()
            if tmdb_logo_widget_view_six is not None:
                tmdb_logo_widget_view_six.clear_cache()
            tmdb_logo_patch_bgra = None
            _warm_tmdb_logo_patch()

            clk = apple_tv_playback_clock
            clk["has_sync"] = False
            clk["playing"] = False
            clk["live_mode"] = False
            clk["latched_content_key"] = None
            clk["latched_total"] = None
            clk["display_played_sec"] = None
            clk["trt_next_fire_mono"] = None
            clk["sync_position"] = 0.0
            clk["sync_mono"] = time.monotonic()

            if status_bar_widget is not None and status_bar_widget.set_accent_from_backdrop_bgr(None):
                _warm_status_bar_blits()

            _sync_status_bar_visibility_for_playback(metadata)

            if scene_enabled:
                last_frame = landing_scene_design_bgr
                if not _PIGEON_EXT:
                    scaled_display = _disp_fit().scale_and_crop(last_frame)
                else:
                    scaled_display = None
            scaled_version += 1
            skip_cache = None
            _refresh_content_indicator()

            if had_art:
                if dev_phase == DevPhase.SETTINGS:
                    sync_developer_chrome()
                else:
                    render_once()

        def _apple_tv_auto_poll_tick() -> None:
            if apple_tv_auto_state.get("running"):
                root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)
                return
            # Avoid overlapping pyatv scan/connect with discover / pairing / probe — reduces spurious TV pairing prompts.
            if apple_tv_busy["active"]:
                root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)
                return
            if not current_apple_tv.get("identifier") or not _PIGEON_EXT:
                _sync_streaming_badge_from_playback_sources(None)
                _sync_status_bar_visibility_for_playback(None)
                root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)
                return
            apple_tv_auto_state["running"] = True
            device_identifier = current_apple_tv.get("identifier", "")
            device_address = current_apple_tv.get("address", "")

            def worker() -> None:
                try:
                    from pigeon.apple_tv_now_playing import fetch_now_playing_info_for_device

                    ok_w, msg_w, metadata_w = fetch_now_playing_info_for_device(
                        device_identifier=device_identifier,
                        device_address=device_address,
                        scan_timeout_s=6,
                    )
                except ImportError:
                    ok_w, msg_w, metadata_w = (
                        False,
                        _pyatv_install_hint(),
                        None,
                    )
                except Exception as e:
                    ok_w, msg_w, metadata_w = False, str(e), None

                # Roku ECP calls can block for multi-second HTTP timeouts; never run them on the Tk thread
                # or the whole UI (including the mic visualizer) freezes on every poll cadence.
                wk_roku_nm: str | None = None
                md_poll_w = metadata_w if isinstance(metadata_w, dict) else None
                md_act_w = md_poll_w is not None and not _atv_metadata_is_content_idle(md_poll_w)
                pyatv_has_app_w = False
                if md_act_w:
                    pyatv_has_app_w = bool(
                        str(md_poll_w.get("app_name") or "").strip()
                        or str(md_poll_w.get("app_id") or "").strip()
                    )
                if not pyatv_has_app_w:
                    try:
                        from pigeon.roku_ecp import (
                            fetch_roku_active_app_name,
                            resolve_roku_ecp_base_url_for_row,
                        )

                        row0_wk = streaming_slot_holder[0]
                        rb_wk = resolve_roku_ecp_base_url_for_row(row0_wk) if row0_wk else ""
                        if rb_wk:
                            t_nm = fetch_roku_active_app_name(rb_wk)
                            if t_nm:
                                wk_roku_nm = t_nm
                    except Exception:
                        wk_roku_nm = None

                pyatv_tmdb_eligible_w = False
                if ok_w and metadata_w:
                    from pigeon.tmdb_poster import is_degenerate_tmdb_query

                    _q_wk = str(metadata_w.get("query") or "").strip()
                    if (
                        _q_wk
                        and not is_degenerate_tmdb_query(_q_wk)
                        and not _atv_metadata_is_content_idle(metadata_w)
                    ):
                        pyatv_tmdb_eligible_w = True

                wk_roku_title: tuple[bool, str, str | None] | None = None
                if not pyatv_tmdb_eligible_w:
                    row0_nf_wk = streaming_slot_holder[0]
                    if row0_nf_wk is not None and not row_is_playback_apple_tv(row0_nf_wk):
                        try:
                            from pigeon.roku_ecp import (
                                fetch_roku_title_for_metadata,
                                resolve_roku_ecp_base_url_for_row,
                            )

                            rb_nf_wk = resolve_roku_ecp_base_url_for_row(row0_nf_wk)
                            if rb_nf_wk:
                                wk_roku_title = fetch_roku_title_for_metadata(rb_nf_wk, timeout=6.0)
                        except Exception:
                            wk_roku_title = None

                def finish() -> None:
                    nonlocal skip_cache
                    apple_tv_auto_state["running"] = False
                    pyatv_ok = bool(ok_w and isinstance(metadata_w, dict))
                    md_for_status: dict[str, object] | None = metadata_w if pyatv_ok else None
                    next_poll_ms = APPLE_TV_POLL_MS
                    if current_apple_tv.get("identifier"):
                        if pyatv_ok:
                            apple_tv_dashboard_track["last_poll_ok"] = True
                            apple_tv_dashboard_track["consecutive_fail"] = 0
                            if isinstance(metadata_w, dict) and _atv_metadata_is_content_idle(metadata_w):
                                # Idle Apple TV metadata does not need aggressive 3s reconnect cadence.
                                next_poll_ms = max(APPLE_TV_POLL_MS, APPLE_TV_IDLE_POLL_MS)
                        else:
                            apple_tv_dashboard_track["last_poll_ok"] = False
                            apple_tv_dashboard_track["consecutive_fail"] = int(
                                apple_tv_dashboard_track.get("consecutive_fail", 0)
                            ) + 1
                            cf = int(apple_tv_dashboard_track.get("consecutive_fail", 0) or 0)
                            # Back off repeated connect attempts to reduce socket churn and UI pressure.
                            next_poll_ms = min(
                                APPLE_TV_FAIL_POLL_MAX_MS,
                                APPLE_TV_POLL_MS * max(2, min(cf, 5)),
                            )
                            if cf == 3:
                                try:
                                    from device_capability_matrix import (
                                        FEATURES as _delegation_feature_rows,
                                        active_device_columns as _adv_dev_cols,
                                    )

                                    lid_log = str(read_current_location_id() or "").strip()
                                    if lid_log:
                                        for _fn, fid in _delegation_feature_rows:
                                            _poll_log = (
                                                f"{_fn}: player poll failed ({cf}\u00d7) while using this "
                                                f"delegation chain \u2014 {str(msg_w or '')[:120]}"
                                            )
                                            append_delegation_log_line(
                                                lid_log,
                                                str(fid),
                                                _poll_log,
                                            )
                                        ndev = len(_adv_dev_cols())
                                        if ndev > 1:
                                            advance_delegation_active(lid_log, "title", ndev)
                                except Exception:
                                    pass
                    try:
                        from pigeon.observed_capability import (
                            update_observed_capabilities_from_player_poll,
                        )

                        _lid_ob = str(read_current_location_id() or "").strip()
                        if _lid_ob and device_identifier and device_address:
                            row_poll: dict[str, str] | None = None
                            for _r in read_saved_streaming_devices_all():
                                if (
                                    str(_r.get("identifier") or "").strip()
                                    == str(device_identifier).strip()
                                    and str(_r.get("address") or "").strip()
                                    == str(device_address).strip()
                                ):
                                    row_poll = dict(_r)
                                    break
                            if row_poll is None:
                                row_poll = {
                                    "identifier": str(device_identifier).strip(),
                                    "address": str(device_address).strip(),
                                    "name": str(current_apple_tv.get("name") or "").strip(),
                                    "label": str(current_apple_tv.get("label") or "").strip(),
                                }
                            update_observed_capabilities_from_player_poll(
                                _lid_ob,
                                row_poll,
                                ok=bool(ok_w),
                                metadata=metadata_w if isinstance(metadata_w, dict) else None,
                            )
                    except Exception:
                        pass
                    _refresh_observed_pairing_led_rows()
                    _refresh_content_indicator()
                    if metadata_w:
                        if ok_w:
                            _update_atv_interaction_from_poll_metadata(metadata_w)
                        prefer_snap = _tmdb_pref_from_metadata(metadata_w)
                        _ppm = str(metadata_w.get("prefer_pyatv_media") or "").strip().lower()
                        if _ppm not in ("auto", "tv", "movie"):
                            _ppm = "auto"
                        # Keep full poll dict for view-4 diagnostics; normalize the fields Pigeon logic relies on.
                        merged_md: dict[str, object] = dict(metadata_w)
                        merged_md["query"] = str(metadata_w.get("query") or "").strip()
                        merged_md["title"] = str(metadata_w.get("title") or "").strip()
                        merged_md["artist"] = str(metadata_w.get("artist") or "").strip()
                        merged_md["series_name"] = str(metadata_w.get("series_name") or "").strip()
                        merged_md["album"] = str(metadata_w.get("album") or "").strip()
                        merged_md["media_type"] = str(metadata_w.get("media_type") or "").strip()
                        merged_md["total_time"] = metadata_w.get("total_time")
                        merged_md["position"] = metadata_w.get("position")
                        merged_md["device_state"] = str(metadata_w.get("device_state") or "").strip()
                        merged_md["inferred_prefer"] = prefer_snap
                        merged_md["prefer_pyatv_media"] = _ppm
                        merged_md["content_key"] = _content_key_from_metadata(metadata_w)
                        merged_md["app_name"] = str(metadata_w.get("app_name") or "").strip()
                        merged_md["app_id"] = str(metadata_w.get("app_id") or "").strip()
                        merged_md["volume_percent"] = metadata_w.get("volume_percent")
                        if ok_w:
                            _bump_clock_saver_significant_device_from_metadata(merged_md)
                        apple_tv_auto_state["last_metadata"] = merged_md
                        _update_status_bar_from_metadata(metadata_w)
                        if playback_overlay_widget is not None:
                            row_av = streaming_slot_holder[0]
                            if row_av and row_is_playback_apple_tv(row_av):
                                from pigeon.widgets.playback_overlay import (
                                    volume_percent_to_widget_line,
                                )

                                v_line = volume_percent_to_widget_line(
                                    metadata_w.get("volume_percent")
                                )
                                # The Denon poll runs on its own short cadence and is the
                                # authoritative source whenever it has produced a usable
                                # reading recently — Apple TV's ``volume_percent`` reads 0
                                # when a physical AV receiver owns the volume, which would
                                # otherwise flash "0" over the correct dB value every
                                # metadata tick. Keep polling (scale-change detection stays
                                # active) but do not let that poll update the widget while
                                # Denon still owns the line.
                                last_denon_usable = float(
                                    denon_vol_cache.get("mono_usable") or 0.0
                                )
                                denon_staleness_s = time.monotonic() - last_denon_usable
                                denon_authoritative = bool(
                                    denon_vol_cache.get("effective")
                                ) and denon_staleness_s < (
                                    RECEIVER_POLL_MS / 1000.0
                                ) * 6
                                if v_line and not denon_authoritative:
                                    old_v = str(receiver_overlay_state.get("volume", ""))
                                    if old_v != v_line:
                                        receiver_overlay_state["volume"] = v_line
                                        _bump_clock_saver_significant_device()
                                        _warm_playback_overlay_blits()
                                        skip_cache = None
                                        render_once()
                    md_poll = metadata_w if isinstance(metadata_w, dict) else None
                    _sync_streaming_badge_from_playback_sources(
                        md_poll,
                        roku_app_name=wk_roku_nm,
                    )
                    pyatv_tmdb_eligible = False
                    if ok_w and metadata_w:
                        from pigeon.tmdb_poster import is_degenerate_tmdb_query

                        query = str(metadata_w.get("query") or "").strip()
                        content_key = _content_key_from_metadata(metadata_w)
                        prefer = _tmdb_pref_from_metadata(metadata_w)
                        if (
                            query
                            and not is_degenerate_tmdb_query(query)
                            and not _atv_metadata_is_content_idle(metadata_w)
                        ):
                            pyatv_tmdb_eligible = True
                            prev_key = apple_tv_auto_state.get("content_key")
                            if content_key and content_key != prev_key:
                                apple_tv_auto_state["content_key"] = content_key
                                apple_tv_auto_state["query"] = query
                                apple_tv_auto_state["prefer"] = prefer
                                spawn_tmdb_poster_fetch(query, prefer=prefer)
                        _return_to_landing_if_atv_idle(metadata_w)
                    if not pyatv_tmdb_eligible and wk_roku_title is not None:
                        try:
                            from pigeon.tmdb_poster import is_degenerate_tmdb_query

                            r_ok, _rmsg, rtitle = wk_roku_title
                            if (
                                r_ok
                                and rtitle
                                and not is_degenerate_tmdb_query(rtitle)
                            ):
                                r_md: dict[str, object] = {
                                    "query": str(rtitle).strip(),
                                    "title": str(rtitle).strip(),
                                    "artist": "",
                                    "series_name": "",
                                    "album": "",
                                    "media_type": "",
                                    "total_time": None,
                                    "position": None,
                                    "device_state": "Playing",
                                    "app_name": str(wk_roku_nm or ""),
                                    "app_id": "",
                                    "prefer_pyatv_media": "auto",
                                }
                                prefer_r = _tmdb_pref_from_metadata(r_md)
                                r_md["inferred_prefer"] = prefer_r
                                r_md["content_key"] = _content_key_from_metadata(r_md)
                                apple_tv_auto_state["last_metadata"] = r_md
                                _update_status_bar_from_metadata(r_md)
                                md_for_status = r_md
                                prev_rk = apple_tv_auto_state.get("content_key")
                                r_ck = r_md.get("content_key")
                                if r_ck and r_ck != prev_rk:
                                    apple_tv_auto_state["content_key"] = r_ck
                                    apple_tv_auto_state["query"] = str(rtitle).strip()
                                    apple_tv_auto_state["prefer"] = "auto"
                                    spawn_tmdb_poster_fetch(
                                        str(rtitle).strip(), prefer="auto"
                                    )
                                if not pyatv_ok:
                                    apple_tv_dashboard_track["last_poll_ok"] = True
                                    apple_tv_dashboard_track["consecutive_fail"] = 0
                        except Exception:
                            pass
                    _sync_status_bar_visibility_for_playback(md_for_status)
                    root.after(max(APPLE_TV_POLL_MS, int(next_poll_ms)), _apple_tv_auto_poll_tick)

                root.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        def on_debug_streaming_slot_apple_tv() -> None:
            if not _PIGEON_EXT:
                messagebox.showinfo("Devices", "Pigeon extensions not loaded.")
                return
            if apple_tv_busy["active"]:
                describe_current_apple_tv(suffix="busy")
                return
            row = streaming_slot_holder[0]
            if row is None:
                _open_find_device_dialog()
                return
            if not row_is_playback_apple_tv(row):
                messagebox.showinfo(
                    "Devices",
                    "Metadata debug applies to Apple TV rows (label shows “Apple TV / tvOS”), not receivers.",
                )
                return
            if not begin_apple_tv_operation("debugging metadata"):
                return

            def worker() -> None:
                try:
                    from pigeon.apple_tv_now_playing import debug_metadata_for_device

                    ok_w, dump_w = debug_metadata_for_device(
                        device_identifier=row["identifier"],
                        device_address=row["address"],
                    )
                except ImportError:
                    ok_w, dump_w = (
                        False,
                        _pyatv_install_hint(),
                    )
                except Exception as e:
                    ok_w, dump_w = False, str(e)

                def finish() -> None:
                    title = "Apple TV Metadata Debug"
                    end_apple_tv_operation()
                    if ok_w:
                        messagebox.showinfo(title, dump_w)
                    else:
                        messagebox.showerror(title, dump_w)

                root.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        def _attach_hover_tooltip(widget: tk.Misc, message: str) -> None:
            tip: list[tk.Toplevel | None] = [None]

            def show(_event: tk.Event | None = None) -> None:
                if tip[0] is not None:
                    return
                tw = tk.Toplevel(root)
                tw.wm_overrideredirect(True)
                try:
                    tw.wm_attributes("-topmost", True)
                except tk.TclError:
                    pass
                x = widget.winfo_rootx() + 4
                y = widget.winfo_rooty() + int(widget.winfo_height()) + 4
                tw.wm_geometry(f"+{x}+{y}")
                tk.Label(
                    tw,
                    text=message,
                    bg="#2a2a30",
                    fg="#e8e8e8",
                    font=S_FONT_SMALL,
                    padx=8,
                    pady=4,
                ).pack()
                tip[0] = tw

            def hide(_event: tk.Event | None = None) -> None:
                if tip[0] is not None:
                    tip[0].destroy()
                    tip[0] = None

            widget.bind("<Enter>", show)
            widget.bind("<Leave>", hide)

        def _perform_tmdb_artwork_retry() -> None:
            if not _PIGEON_EXT:
                return
            rules = [
                ("movie", "primary", "movie+primary"),
                ("tv", "primary", "tv+primary"),
                ("auto", "alternate", "auto+alternate_query"),
                ("auto", "primary", "auto+primary"),
            ]
            idx = tmdb_retry_rule_idx[0] % len(rules)
            prefer, qsource, rule_id = rules[idx]
            primary = str(apple_tv_auto_state.get("query") or "").strip()
            md_raw = apple_tv_auto_state.get("last_metadata")
            md = md_raw if isinstance(md_raw, dict) else {}
            alt = _alternate_tmdb_query_from_metadata(md if md else None, primary)
            if qsource == "primary":
                q = primary
            else:
                q = (alt or primary).strip()
            if not q:
                messagebox.showwarning(
                    "TMDb retry",
                    "No playback search query yet. Play something on the device and wait for metadata, "
                    "or type a query in the command bar (tmdb …).",
                )
                return
            tmdb_retry_rule_idx[0] = idx + 1
            entry = {
                "event": "tmdb_retry_hotkey",
                "rule_index": idx,
                "rule_id": rule_id,
                "prefer": prefer,
                "query_source": qsource,
                "query_sent": q,
                "primary_query": primary,
                "alternate_available": bool(alt),
                "alternate_query": alt,
                "active_tmdb_title_key_before": active_tmdb_title_key,
                "active_tmdb_display_title_before": active_tmdb_display_title,
                "apple_tv_auto_prefer": apple_tv_auto_state.get("prefer"),
                "content_key": apple_tv_auto_state.get("content_key"),
                "live_mode": apple_tv_playback_clock.get("live_mode"),
                "metadata_excerpt": {
                    k: md.get(k)
                    for k in ("title", "artist", "series_name", "media_type", "inferred_prefer", "device_state")
                    if md.get(k)
                },
            }
            _tmdb_retry_log_append(entry)
            ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
            was = active_tmdb_display_title or "—"
            _append_tmdb_retry_log_ui(f"{ts}  {rule_id}  prefer={prefer}  q={q!r}  was={was!r}")
            spawn_tmdb_poster_fetch(q, prefer=prefer)
            sys.stderr.write(f"pigeon: tmdb retry ({rule_id}) prefer={prefer} q={q!r}\n")
            sys.stderr.flush()

        def on_tmdb_retry_hotkey(event: tk.Event) -> str | None:
            _bump_pigeon_user_activity(event)
            if not _PIGEON_EXT:
                return None
            if _widget_accepts_typing(event.widget):
                return None
            now_hk = time.monotonic()
            if now_hk - _last_tmdb_hotkey_mono[0] < 0.15:
                return "break"
            _last_tmdb_hotkey_mono[0] = now_hk
            _perform_tmdb_artwork_retry()
            return "break"

        def on_tmdb_quality_error_report_hotkey(event: tk.Event) -> str | None:
            """Toggle TMDb artwork error flag for the next scored populate (⌘⇧X on macOS)."""
            _bump_pigeon_user_activity(event)
            if not _PIGEON_EXT:
                return None
            if _widget_accepts_typing(event.widget):
                return None
            ks = (getattr(event, "keysym", "") or "").lower()
            if ks != "x":
                return None
            st = int(getattr(event, "state", 0))
            shift = bool(st & 0x0001)
            meta_cmd = (
                bool(st & 0x100000)
                or bool(st & 0x080000)
                or bool(st & 0x0008)
                or bool(st & 0x20000)
            )
            ctrl = bool(st & 0x0004)
            # macOS: ⌘⇧X (primary). Any OS: Ctrl+Shift+X also accepted when that binding fires.
            if not shift or not (meta_cmd or ctrl):
                return None
            now_q = time.monotonic()
            if now_q - _last_tmdb_quality_report_mono[0] < 0.15:
                return "break"
            _last_tmdb_quality_report_mono[0] = now_q
            tmdb_quality_error_flag[0] = not bool(tmdb_quality_error_flag[0])
            is_flagged = bool(tmdb_quality_error_flag[0])
            _trigger_tmdb_quality_toggle_overlay("flag" if is_flagged else "undo")
            try:
                if is_flagged:
                    sys.stderr.write(
                        "pigeon: TMDb material quality issue flagged (⌘⇧X / Ctrl+Shift+X). "
                        "The next successful TMDb populate counts as a failure; scored events append to "
                        f"{PIGEON_STATE_DIR_TILDE}/tmdb_quality_event_reports.log\n"
                    )
                else:
                    sys.stderr.write(
                        "pigeon: TMDb material quality issue flag cleared (⌘⇧X / Ctrl+Shift+X). "
                        "The next successful TMDb populate now counts as a success unless flagged again.\n"
                    )
                sys.stderr.flush()
            except Exception:
                pass
            return "break"

        purge_image_media_btn = tk.Button(
            content_buttons_row,
            text="Purge Image Media",
            command=on_purge_image_media,
            font=S_FONT_BTN,
            padx=8,
            pady=4,
        )
        purge_image_media_btn.pack(side=tk.LEFT, padx=(0, 8))
        if _PIGEON_EXT:
            _mq_glance = tk.Label(
                content_buttons_row,
                text="",
                fg="#e4e6ef",
                bg="#111",
                font=(_S, 13, "bold"),
                anchor=tk.W,
                justify=tk.LEFT,
            )
            _mq_glance.pack(side=tk.LEFT, padx=(8, 0), anchor=tk.CENTER)
            match_quality_glance_label_holder[0] = _mq_glance
            _attach_hover_tooltip(
                _mq_glance,
                "Scored when TMDb material changes after a successful populate: fail if ⌘⇧X / Ctrl+Shift+X "
                "was toggled on before that fetch, else success. Persisted in state.json; details + log on "
                "Advanced → TMDb.",
            )
            reset_mq_stats_btn = tk.Button(
                content_buttons_row,
                text="Reset match stats",
                command=on_reset_tmdb_match_quality_stats,
                font=S_FONT_BTN,
                padx=8,
                pady=4,
            )
            reset_mq_stats_btn.pack(side=tk.LEFT, padx=(10, 0))
            _attach_hover_tooltip(
                reset_mq_stats_btn,
                "Clears the ok / fail counts and % shown here (saved in state.json). Does not change "
                "tmdb_error_report.numbers, pigeonTMDBReport.csv, or tmdb_quality_event_reports.log.",
            )
            root.after_idle(_refresh_match_quality_glance_label)

        settings_footer_row = tk.Frame(settings_inner, bg="#111")
        settings_footer_row.pack(anchor=tk.W, fill=tk.X, pady=(16, 12))
        _frb = tk.Button(
            settings_footer_row,
            text="Reset",
            command=on_reset_pigeon_devices_and_media,
            font=S_FONT_BTN,
            padx=14,
            pady=6,
        )
        _frb.pack(side=tk.LEFT, padx=(0, 12))
        settings_footer_reset_holder[0] = _frb
        _fdb = tk.Button(
            settings_footer_row,
            text="Debug metadata",
            command=on_debug_streaming_slot_apple_tv,
            font=S_FONT_BTN,
            padx=10,
            pady=4,
        )
        _fdb.pack(side=tk.LEFT, padx=(0, 0))
        settings_footer_debug_holder[0] = _fdb
        tk.Label(
            settings_footer_row,
            text=f"Version {version_string()}",
            fg="#6d6d75",
            bg="#111",
            font=S_FONT_BODY,
        ).pack(side=tk.RIGHT, anchor=tk.E)
        _attach_hover_tooltip(
            _frb,
            "Clears all saved devices, pyatv credentials, discovery cache, and purges pigeonTMDB originals/backdrops/title-treatments.",
        )

        migrate_device_slots_from_legacy_if_needed()
        merge_legacy_saved_receivers_into_av_slot()
        streaming_slot_holder[0] = read_saved_streaming_device()
        avr_slot_holder[0] = read_saved_av_receiver()
        receiver_http_host["host"] = str(read_last_receiver().get("host") or "").strip()
        describe_current_apple_tv()
        _refresh_location_selector()
        _rebuild_paired_devices_panel()
        root.after(300, _schedule_refresh_pairing_leds)
        root.after(2500, _apple_tv_auto_poll_tick)
        root.after(PLAYBACK_UI_TICK_MS, _playback_ui_tick)

        def submit_command_entry(_event=None) -> str:
            nonlocal skip_cache
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS) and display_view_holder[0] != DisplayView.FIVE:
                return "break"
            _bump_pigeon_user_activity()
            now_sub = time.monotonic()
            if now_sub - _last_command_submit_mono[0] < 0.2:
                return "break"
            _last_command_submit_mono[0] = now_sub
            text = command_entry.get().strip()
            key = text.lower()
            if text and _PIGEON_EXT:
                m_tmdb = re.match(r"(?i)tmdb\s+(?P<q>.+)$", text)
                if m_tmdb:
                    qrest = m_tmdb.group("q").strip()
                    if qrest:
                        q2, pref = parse_tmdb_command_phrase(qrest)
                        if q2:
                            spawn_tmdb_poster_fetch(q2, prefer=pref)
                    else:
                        sys.stderr.write("pigeon: tmdb: empty query (use: tmdb Movie Title)\n")
                        sys.stderr.flush()
                else:
                    # Plain title or tv/movie hint — TMDb (auto picks movie vs TV by popularity)
                    q2, pref = parse_tmdb_command_phrase(text)
                    if q2:
                        spawn_tmdb_poster_fetch(q2, prefer=pref)
            elif text:
                sys.stderr.write(f"pigeon: command: {text}\n")
                sys.stderr.flush()
            command_entry.delete(0, tk.END)
            hide_command_entry()
            return "break"

        for _seq in ("<Return>", "<KeyPress-Return>"):
            command_entry.bind(_seq, submit_command_entry)
        command_entry.bind("<KP_Enter>", submit_command_entry)
        command_entry.bind("<KeyPress-KP_Enter>", submit_command_entry)

        label.bind("<Button-1>", on_click_focus, add="+")
        # Tap-to-wake: some platforms deliver release more reliably for “tap” than press alone.
        # While the saver layer is visible, a tap brightens it briefly (see CLOCK_SAVER_PEEK_S); view 3 is always on.
        def _on_label_button_release_peek_or_bump(event: tk.Event) -> None:
            nonlocal skip_cache
            if _PIGEON_EXT and clock_saver_composite_bgra is not None:
                now_e = time.monotonic()
                if _clock_saver_for_compose(now_e):
                    clock_saver_peek_until_mono[0] = now_e + CLOCK_SAVER_PEEK_S
                    _bump_pigeon_user_activity(event)
                    skip_cache = None
                    try:
                        render_once()
                    except Exception:
                        pass
                    return
            _bump_pigeon_user_activity(event)

        label.bind("<ButtonRelease-1>", _on_label_button_release_peek_or_bump, add="+")
        label.bind("<Double-Button-1>", on_double_click_scene, add="+")
        label.bind("<Button-3>", lambda e: try_cycle_dev_phase(None))

        # Omit command_entry: it needs local <Return> to submit before bind_all runs.
        for w in (
            root,
            shell,
            video_area,
            label,
            command_bar,
            settings_frame,
            settings_scroll_outer,
            settings_canvas,
            settings_inner,
            settings_footer_row,
        ):
            if w is not None:
                _prepend_hotkey_bindtag(w)

        for seq in ("<KeyPress-Tab>", "<Key-Tab>"):
            root.bind_class(HOTKEY_BINDTAG, seq, on_tab_key)
        root.bind_class(HOTKEY_BINDTAG, "<Control-KeyPress-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<Control-Key-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-s>", on_s_key)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-S>", on_s_key)

        def on_return_overlay_command(event: tk.Event) -> str | None:
            _bump_pigeon_user_activity(event)
            w = event.widget
            if w == command_entry or str(w) == str(command_entry):
                return None
            if _widget_accepts_typing(w):
                return None
            if dev_phase in (DevPhase.GRID, DevPhase.SETTINGS) or display_view_holder[0] == DisplayView.FIVE:
                if command_entry_visible:
                    try:
                        command_entry.focus_force()
                    except tk.TclError:
                        command_entry.focus_set()
                else:
                    show_command_entry()
                return "break"
            if _PIGEON_EXT:
                from pigeon.player_remote import queue_player_remote_action

                queue_player_remote_action(
                    streaming_slot_holder[0],
                    current_apple_tv=current_apple_tv,
                    action="select",
                    apple_tv_busy=apple_tv_busy,
                )
                return "break"
            return None

        root.bind_all("<KeyPress-Tab>", on_tab_key)
        root.bind_all("<Key-Tab>", on_tab_key)
        # Do not bind_all(Return): on macOS that can run before the Entry binding and swallow the key.
        for _rseq in ("<Return>", "<KeyPress-Return>", "<KP_Enter>", "<KeyPress-KP_Enter>"):
            root.bind_class(HOTKEY_BINDTAG, _rseq, on_return_overlay_command)

        def _send_player_play_pause_hotkey() -> bool:
            """
            If a **Player** slot is set, send play/pause on a worker thread (Apple TV: pyatv;
            Roku: ECP). Returns True when a send was queued (so Space should not fall through).
            """
            if not _PIGEON_EXT:
                return False
            if apple_tv_busy["active"]:
                return False
            row = streaming_slot_holder[0]
            if not row:
                return False
            if row_is_playback_apple_tv(row):
                ident = str(current_apple_tv.get("identifier") or "").strip() or str(
                    row.get("identifier") or ""
                ).strip()
                addr = str(current_apple_tv.get("address") or "").strip() or str(
                    row.get("address") or ""
                ).strip()
                if not ident:
                    return False
                if not addr:
                    addr = ident

                try:
                    from pigeon.apple_tv_now_playing import enqueue_apple_tv_remote_command

                    if enqueue_apple_tv_remote_command(
                        device_identifier=ident,
                        device_address=addr,
                        method_name="play_pause",
                        scan_timeout_s=3,
                    ):
                        return True
                except Exception:
                    pass
                return False
            try:
                from pigeon.roku_ecp import resolve_roku_ecp_base_url_for_row, roku_send_play_pause

                rbase = str(resolve_roku_ecp_base_url_for_row(row) or "").strip()
                if not rbase:
                    return False
            except Exception:
                return False

            def _work_roku() -> None:
                try:
                    from pigeon.roku_ecp import roku_send_play_pause

                    roku_send_play_pause(base_url=rbase, timeout=3.0)
                except Exception:
                    pass

            threading.Thread(target=_work_roku, daemon=True).start()
            return True

        def on_space_play(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            _bump_pigeon_user_activity(event)
            now = time.monotonic()
            if now - _last_space_mono[0] < 0.12:
                return "break"
            _last_space_mono[0] = now
            if _send_player_play_pause_hotkey():
                return "break"
            # After a TMDb fetch, bring backdrop + title logo to the screen (toggle_play often no-ops here).
            if saved_backdrop_master_bgr is not None and not use_backdrop_scene:
                apply_saved_tmdb_backdrop_to_display()
                return "break"
            toggle_play()
            return "break"

        # <KeyPress-Space> is invalid on some Tk builds (TclError: bad keysym "Space").
        for _space_seq in ("<space>", "<KeyPress-space>"):
            root.bind_all(_space_seq, on_space_play)

        def on_display_view_digit(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            if not _PIGEON_EXT:
                return None
            ch = getattr(event, "char", "") or ""
            if ch not in "123456":
                return None
            nonlocal skip_cache
            if ch == "1" and display_view_holder[0] == DisplayView.ONE:
                # viewOne.07/.08/.09 have no alternate layout — ignore the toggle.
                _vv_now = _current_view_one_variant()
                if (
                    _vv_now is not None
                    and variant_has_alternate is not None
                    and not variant_has_alternate(_vv_now)
                ):
                    _bump_pigeon_user_activity(event)
                    return "break"
                _cur = int(view_one_layout_holder[0])
                if _cur == int(ViewOneLayout.PIGEON_FULL):
                    view_one_layout_holder[0] = int(ViewOneLayout.PIGEON_SIMPLE)
                elif _cur == int(ViewOneLayout.PIGEON_SIMPLE):
                    view_one_layout_holder[0] = int(ViewOneLayout.PIGEON_POSTER)
                else:
                    view_one_layout_holder[0] = int(ViewOneLayout.PIGEON_FULL)
                skip_cache = None
                _bump_pigeon_user_activity(event)
                return "break"
            if ch == "4" and display_view_holder[0] == DisplayView.FOUR:
                view_four_subview_holder[0] = (int(view_four_subview_holder[0]) + 1) % 3
                skip_cache = None
                _bump_pigeon_user_activity(event)
                return "break"
            display_view_holder[0] = DisplayView(int(ch))
            if ch == "1":
                view_one_layout_holder[0] = int(ViewOneLayout.PIGEON_FULL)
            if ch == "4":
                view_four_subview_holder[0] = 0
            skip_cache = None
            _bump_pigeon_user_activity(event)
            return "break"

        for _dv_ch in ("1", "2", "3", "4", "5", "6"):
            root.bind_all(f"<KeyPress-{_dv_ch}>", on_display_view_digit)

        def on_arrow_remote(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            if not _PIGEON_EXT:
                return None
            ks = getattr(event, "keysym", "") or ""
            if ks not in ("Up", "Down", "Left", "Right"):
                return None
            from pigeon.player_remote import queue_player_remote_action

            st = int(getattr(event, "state", 0))
            if st & 0x0004:
                return None
            sh = bool(st & 0x0001)
            meta_cmd = (
                bool(st & 0x100000)
                or bool(st & 0x080000)
                or bool(st & 0x0008)
                or bool(st & 0x20000)
            )
            row = streaming_slot_holder[0]
            if meta_cmd:
                cmd_map = {
                    "Up": "power_on",
                    "Down": "power_off",
                    "Left": "back",
                    "Right": "home",
                }
                act = cmd_map.get(ks)
                if act:
                    queue_player_remote_action(
                        row,
                        current_apple_tv=current_apple_tv,
                        action=act,
                        apple_tv_busy=apple_tv_busy,
                    )
                return "break"
            if sh:
                if ks == "Up":
                    queue_player_remote_action(
                        row,
                        current_apple_tv=current_apple_tv,
                        action="volume_up",
                        apple_tv_busy=apple_tv_busy,
                    )
                elif ks == "Down":
                    queue_player_remote_action(
                        row,
                        current_apple_tv=current_apple_tv,
                        action="volume_down",
                        apple_tv_busy=apple_tv_busy,
                    )
                elif ks == "Left":
                    queue_player_remote_action(
                        row,
                        current_apple_tv=current_apple_tv,
                        action="skip_back",
                        apple_tv_busy=apple_tv_busy,
                    )
                elif ks == "Right":
                    queue_player_remote_action(
                        row,
                        current_apple_tv=current_apple_tv,
                        action="skip_fwd",
                        apple_tv_busy=apple_tv_busy,
                    )
                return "break"
            nav = {
                "Up": "nav_up",
                "Down": "nav_down",
                "Left": "nav_left",
                "Right": "nav_right",
            }.get(ks)
            if nav:
                queue_player_remote_action(
                    row,
                    current_apple_tv=current_apple_tv,
                    action=nav,
                    apple_tv_busy=apple_tv_busy,
                )
            return "break"

        for _ak in ("<KeyPress-Up>", "<KeyPress-Down>", "<KeyPress-Left>", "<KeyPress-Right>"):
            root.bind_all(_ak, on_arrow_remote)

        def on_escape(event: tk.Event) -> str | None:
            _bump_pigeon_user_activity(event)
            if command_entry_visible:
                hide_command_entry()
                return "break"
            quit_app()
            return "break"

        root.bind_all("<Escape>", on_escape)
        root.bind_all("<KeyPress-F9>", lambda e: try_cycle_dev_phase(None))
        root.bind_all("<KeyPress-F10>", on_f10_key)

        def on_ctrl_shift_tab_advanced(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            if not _PIGEON_EXT:
                return None
            st = int(getattr(event, "state", 0))
            if not (st & 0x0004) or not (st & 0x0001):
                return None
            _bump_pigeon_user_activity(event)
            now = time.monotonic()
            if now - _last_adv_shift_tab_mono[0] < 0.35:
                return "break"
            _last_adv_shift_tab_mono[0] = now
            _open_advanced_capability_matrix()
            return "break"

        for _adv_hot in (
            "<Control-Shift-KeyPress-Tab>",
            "<Control-Shift-Key-Tab>",
            "<Control-Shift-KeyPress-ISO_Left_Tab>",
        ):
            root.bind_all(_adv_hot, on_ctrl_shift_tab_advanced)
        for _stab in (
            "<KeyPress-ISO_Left_Tab>",
            "<Shift-KeyPress-Tab>",
            "<Shift-Key-Tab>",
        ):
            root.bind_all(_stab, on_shift_tab_dev_cycle)
        for _tmdb_key in ("<KeyPress-question>", "<Shift-KeyPress-slash>"):
            root.bind_all(_tmdb_key, on_tmdb_retry_hotkey)
        for _tmdb_qx in (
            "<Command-Shift-KeyPress-x>",
            "<Command-Shift-KeyPress-X>",
            "<Control-Shift-KeyPress-x>",
            "<Control-Shift-KeyPress-X>",
        ):
            root.bind_all(_tmdb_qx, on_tmdb_quality_error_report_hotkey)

        def on_tmdb_match_mode_toggle(event: tk.Event) -> str | None:
            _bump_pigeon_user_activity(event)
            if not _PIGEON_EXT:
                return None
            if _widget_accepts_typing(event.widget):
                return None
            now_tm = time.monotonic()
            if now_tm - _last_tmdb_match_toggle_mono[0] < 0.2:
                return "break"
            _last_tmdb_match_toggle_mono[0] = now_tm
            try:
                from pigeon.tmdb_poster import toggle_tmdb_match_mode
            except ImportError:
                return None
            mode = toggle_tmdb_match_mode()
            sys.stderr.write(f"pigeon: TMDb title match: {mode} (Ctrl+Shift+M to toggle)\n")
            sys.stderr.flush()
            return "break"

        def on_dev_series_title_training_hotkey(event: tk.Event) -> str | None:
            """Dev-only: map current playback metadata fingerprint → series title (training JSON)."""
            _bump_pigeon_user_activity(event)
            if not _PIGEON_EXT:
                return None
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS) and display_view_holder[0] != DisplayView.FIVE:
                return None
            if _widget_accepts_typing(event.widget):
                return None
            lm = apple_tv_auto_state.get("last_metadata")
            if not isinstance(lm, dict) or not any(
                str(lm.get(k) or "").strip()
                for k in ("title", "series_name", "artist", "album", "query")
            ):
                messagebox.showinfo(
                    "Series title training",
                    "No playback metadata snapshot yet. Start playback and wait for a poll, then try again.",
                    parent=root,
                )
                return "break"
            try:
                from pigeon.raw_title import raw_title_from_metadata_dict
                from pigeon.series_title_training import add_training_mapping
            except ImportError:
                messagebox.showinfo(
                    "Series title training",
                    "Training modules are not available in this build.",
                    parent=root,
                )
                return "break"

            rt = raw_title_from_metadata_dict(lm)
            sig = rt.training_signature_normalized()
            if not sig:
                messagebox.showinfo(
                    "Series title training",
                    "Could not build a stable fingerprint from the current metadata.",
                    parent=root,
                )
                return "break"

            tw = tk.Toplevel(root)
            tw.title("Series title training")
            tw.transient(root)
            tk.Label(
                tw,
                text="Map this playback fingerprint to a TMDb series title.\n"
                f"Saved under {PIGEON_STATE_DIR_TILDE}/series_title_training_hints.json",
                justify="center",
            ).pack(padx=12, pady=(10, 4))
            preview = sig[:180] + ("…" if len(sig) > 180 else "")
            tk.Label(
                tw,
                text=f"Key: {preview}",
                fg="#888",
                wraplength=420,
                justify="left",
            ).pack(padx=12, pady=4)
            ent = tk.Entry(tw, width=48)
            ent.pack(padx=12, pady=6)
            hint = (rt.layer_series_title or rt.raw_series_name or rt.raw_title or "").strip()
            if hint:
                ent.insert(0, hint)

            def _save_training() -> None:
                q_sp = ent.get().strip()
                ok_h, msg_h = add_training_mapping(sig, q_sp)
                if ok_h:
                    sys.stderr.write(f"pigeon: series title training: {msg_h}\n")
                    sys.stderr.flush()
                    tw.destroy()
                    if q_sp:
                        spawn_tmdb_poster_fetch(q_sp, prefer=str(apple_tv_auto_state.get("prefer") or "auto"))
                else:
                    messagebox.showerror("Series title training", msg_h, parent=tw)

            bf = tk.Frame(tw)
            bf.pack(pady=(4, 12))
            tk.Button(bf, text="Save & refetch TMDb", command=_save_training).pack(side=tk.LEFT, padx=6)
            tk.Button(bf, text="Cancel", command=tw.destroy).pack(side=tk.LEFT, padx=6)
            root.after_idle(lambda: ent.focus_set())
            return "break"

        for _plus_key in (
            "<KeyPress-plus>",
            "<Shift-KeyPress-equal>",
            "<Shift-KeyPress-plus>",
            "<KeyPress-KP_Add>",
        ):
            root.bind_all(_plus_key, on_dev_series_title_training_hotkey)

        root.bind_all("<Control-Shift-KeyPress-m>", on_tmdb_match_mode_toggle)
        root.bind_all("<Control-Shift-KeyPress-M>", on_tmdb_match_mode_toggle)

        root.bind_all("<Control-Shift-KeyPress-s>", lambda e: toggle_scene(require_overlay=False))
        root.bind_all("<Control-Shift-KeyPress-S>", lambda e: toggle_scene(require_overlay=False))

        def _focus_when_mapped(_event=None) -> None:
            try:
                root.focus_force()
            except tk.TclError:
                root.focus_set()

        root.bind("<Map>", _focus_when_mapped)
        root.after_idle(_focus_when_mapped)

        for _pigeon_act in ("<Button-1>", "<B1-Motion>", "<KeyPress>"):
            root.bind_all(_pigeon_act, _bump_pigeon_user_activity, add="+")

        def _apply_shell_size(w: int, h: int) -> None:
            nonlocal skip_cache, black_photo, scaled_display, scaled_version
            if w < 32 or h < 32:
                return
            if display_dims[0] == w and display_dims[1] == h:
                return
            display_dims[0] = w
            display_dims[1] = h
            fit_holder[0] = SceneFit(target_w=w, target_h=h)
            black_photo = None
            skip_cache = None
            if use_backdrop_scene and backdrop_master_bgr is not None and not _PIGEON_EXT:
                from pigeon.image_ui_protocol import backdrop_scene_bgr_for_display

                scaled_display = backdrop_scene_bgr_for_display(
                    backdrop_master_bgr,
                    w,
                    h,
                    app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit,
                    app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
                )
                scaled_version += 1
            sync_developer_chrome()

        def _on_shell_configure(event: tk.Event) -> None:
            if event.widget is not shell:
                return
            w, h = int(event.width), int(event.height)
            # Apply on every configure so chrome (buttons, HUD, bars) tracks live resize;
            # debounced after() only ran after drag ended.
            _apply_shell_size(w, h)

        sync_developer_chrome()
        if _PIGEON_EXT and dev_phase == DevPhase.OFF:
            _start_location_toast(startup=True)

        def render_once() -> None:
            nonlocal last_frame, brightness_current, scaled_display, scaled_version, skip_cache, black_photo
            nonlocal scene_enabled, playing, use_backdrop_scene, backdrop_master_bgr

            if _render_after_id[0] is not None:
                try:
                    root.after_cancel(_render_after_id[0])
                except tk.TclError:
                    pass
                _render_after_id[0] = None

            _render_tick_t0 = time.perf_counter()
            now = time.monotonic()
            _intro_mono = mic_viz_intro_start_mono[0]

            def _mic_launch_needs_fast_composite() -> bool:
                if not _PIGEON_EXT or _intro_mono is None or _blend_mic_visualizer is None:
                    return False
                evlf = _effective_display_view()
                if evlf in (DisplayView.THREE, DisplayView.FOUR):
                    return False
                t_r = now - _intro_mono
                if t_r < float(MIC_VIZ_INTRO_TOTAL_S):
                    return True
                if mic_viz_launch_descend_latched[0] == 1:
                    if (t_r - float(MIC_VIZ_INTRO_TOTAL_S)) < float(MIC_VIZ_LAUNCH_DESCENT_S):
                        return True
                return False

            def _schedule_next_render() -> None:
                elapsed_ms = int((time.perf_counter() - _render_tick_t0) * 1000.0)
                interval = _next_render_ms()
                delay = max(interval, elapsed_ms + 1)
                _schedule_render_oneshot(delay)

            def _next_render_ms() -> int:
                base = frame_interval_ms if playing else paused_interval_ms
                if (
                    _PIGEON_EXT
                    and _blend_mic_visualizer is not None
                    and (
                        not _backdrop_active_for_view()
                        or _mic_launch_needs_fast_composite()
                    )
                ):
                    if not playing:
                        return max(base, MIC_VIZ_IDLE_COMPOSITE_MS)
                    return min(base, _mic_viz_composite_ms)
                return base
            # With ext + splash, only count this window **after** splash removal (same t0 as mic intro).
            # Using ``_pigeon_ui_started_mono`` here could fire mid-splash (e.g. auto-restore backdrop under the overlay).
            _startup_elapsed = -1.0
            if _PIGEON_EXT:
                _startup_elapsed = (now - _intro_mono) if _intro_mono is not None else -1.0
            if (
                _PIGEON_EXT
                and not _startup_splash_complete[0]
                and _startup_elapsed >= STARTUP_PIGEON_WORDMARK_MAX_S
                and dev_phase != DevPhase.SETTINGS
            ):
                _startup_splash_complete[0] = True
                if (
                    STARTUP_AUTO_RESTORE_SAVED_BACKDROP
                    and saved_backdrop_master_bgr is not None
                    and scene_enabled
                    and not use_backdrop_scene
                ):
                    apply_saved_tmdb_backdrop_to_display()
                    # Inner ``render_once`` schedules the loop, but guarantee a timer if that path returns early.
                    _schedule_next_render()
                    return
                skip_cache = None
                _warm_playback_overlay_blits()

            if dev_phase == DevPhase.SETTINGS:
                _schedule_render_oneshot(paused_interval_ms)
                return

            if _PIGEON_EXT:
                _compose_idle_strength_holder[0] = _update_idle_dim_strength(now)
            else:
                _compose_idle_strength_holder[0] = 0.0
            t = (now - brightness_t0) / brightness_duration_s if brightness_duration_s > 0 else 1.0
            if t <= 0.0:
                brightness_current = brightness_from
            elif t >= 1.0:
                brightness_current = brightness_target
            else:
                brightness_current = brightness_from + (brightness_target - brightness_from) * t

            if not scene_enabled:
                if _PIGEON_EXT:
                    out_bgr = _compose_shown_frame(None, 1.0)
                    if lerp_bgr_red_monochrome is not None:
                        sm = max(0.0, min(1.0, _compose_idle_strength_holder[0]))
                        if sm > 1e-6:
                            out_bgr = lerp_bgr_red_monochrome(out_bgr, sm)
                    _update_label_photo_from_bgr(label, out_bgr, label_live_photo)
                else:
                    if black_photo is None:
                        black_photo = _bgr_to_tk_image(_black_screen_bgr())
                    label.configure(image=black_photo)
                    label.image = black_photo
                _schedule_next_render()
                return

            if _backdrop_active_for_view():
                if backdrop_master_bgr is None:
                    use_backdrop_scene = False
            if not _backdrop_active_for_view():
                if last_frame is None:
                    _schedule_next_render()
                    return
                if not _PIGEON_EXT and scaled_display is None:
                    _schedule_next_render()
                    return

            brightness_animating = abs(brightness_current - brightness_target) > 1e-4
            # TMDb backdrop: fixed level; paused video uses 0.3.
            _backdrop_active = _backdrop_active_for_view()
            b_scene = BACKDROP_BRIGHTNESS if _backdrop_active else brightness_current
            b_key = round(float(b_scene), 4)
            # Clock text changes every second; include wall time when widgets are active.
            tick_key = int(time.time()) if _PIGEON_EXT else 0
            idle_s_here = (
                max(0.0, min(1.0, _compose_idle_strength_holder[0])) if _PIGEON_EXT else 0.0
            )
            idle_want_here = (
                (1.0 if _atv_idle_monochrome_active() else 0.0)
                if THEATER_IDLE_DIM_ENABLED
                else 0.0
            )
            # While easing toward dim or back to full bright, always composite (skip-cache can quantize away steps).
            idle_dim_animating = _PIGEON_EXT and abs(idle_s_here - idle_want_here) > 1e-4
            idle_cache_key = int(round(idle_s_here * 500)) if _PIGEON_EXT else 0
            ta_toast = _location_toast_alpha(now) if _PIGEON_EXT else 0.0
            location_toast_animating = _PIGEON_EXT and 0.0 < ta_toast < 1.0
            location_toast_cache_key = int(round(ta_toast * 1000)) if _PIGEON_EXT else 0
            clock_saver_cache_key = 1 if (_PIGEON_EXT and _clock_saver_for_compose(now)) else 0
            clock_saver_peek_cache_key = (
                1 if (_PIGEON_EXT and now < clock_saver_peek_until_mono[0]) else 0
            )
            # Mic EQ intro is driven by per-frame ``mic_viz_cache_key``; no separate wordmark phase.
            startup_wm_cache_key = 0
            paused_row_cache_key = 1 if (_PIGEON_EXT and _show_paused_row_overlay()) else 0
            _ev_mic = _effective_display_view() if _PIGEON_EXT else DisplayView.ONE
            _mic_launch_fast = _mic_launch_needs_fast_composite() if _PIGEON_EXT else False
            _mic_past_launch_descent_hide = False
            if (
                _PIGEON_EXT
                and _intro_mono is not None
                and mic_viz_launch_descend_latched[0] == 1
                and _ev_mic not in (DisplayView.TWO, DisplayView.THREE, DisplayView.FOUR)
            ):
                t_hide = now - _intro_mono
                if t_hide >= float(MIC_VIZ_INTRO_TOTAL_S) + float(MIC_VIZ_LAUNCH_DESCENT_S):
                    _mic_past_launch_descent_hide = bool(_backdrop_active)
            _mic_on = (
                _PIGEON_EXT
                and _blend_mic_visualizer is not None
                and _ev_mic not in (DisplayView.THREE, DisplayView.FOUR)
                and not _mic_past_launch_descent_hide
                and (_ev_mic == DisplayView.TWO or not _backdrop_active or _mic_launch_fast)
            )
            mic_viz_cache_key = (
                int(now * _MIC_VIZ_COMPOSITE_FPS) if _mic_on else 0
            )
            mic_eq_needs_composite = _mic_on
            if _PIGEON_EXT and status_bar_widget is not None:
                if status_bar_widget.set_theater_dim_suppressed(idle_s_here >= 0.5):
                    _warm_status_bar_blits()
            theater_dim_key = (
                1
                if (
                    _PIGEON_EXT
                    and status_bar_widget is not None
                    and status_bar_widget.theater_dim_suppressed
                )
                else 0
            )
            # Receiver overlay text must bust skip-cache when paused/backdrop.
            receiver_overlay_skip_sig = ""
            if _PIGEON_EXT:
                _set_playback_overlay_clock_saver_volume_flag()
                receiver_overlay_skip_sig = "\x1e".join(
                    str(receiver_overlay_state.get(k, "")) for k in ("incoming", "config", "volume")
                )
                receiver_overlay_skip_sig += "\x1e" + (
                    f"{int(bool(playback_overlay_flags.get('clock_saver_volume_only')))}"
                    f"{int(bool(playback_overlay_flags.get('clock_saver_netflix_full_overlay')))}"
                )
            (_tmdb_x_bgr, _tmdb_x_alpha, _tmdb_x_caption, _tmdb_x_phase) = _tmdb_quality_toggle_overlay_state(now)
            tmdb_x_animating = _tmdb_x_alpha > 1e-6
            tmdb_x_cache_key = (
                int(round(_tmdb_x_alpha * 1000.0))
                + (int(_tmdb_x_phase) * 2000)
                + (1 if tuple(_tmdb_x_bgr) == (0, 0, 255) else 0)
            )
            tmdb_flag_badge_on = bool(tmdb_quality_error_flag[0])
            tmdb_flag_badge_cache_key = 1 if tmdb_flag_badge_on else 0

            if (
                not playing
                and not mic_eq_needs_composite
                and not brightness_animating
                and not idle_dim_animating
                and not location_toast_animating
                and not tmdb_x_animating
                and skip_cache
                == (
                    scaled_version,
                    b_key,
                    int(dev_phase),
                    int(display_view_holder[0]),
                    int(_effective_display_view()),
                    int(view_one_layout_holder[0]),
                    int(view_four_subview_holder[0]),
                    tick_key,
                    display_dims[0],
                    display_dims[1],
                    1 if _backdrop_active else 0,
                    idle_cache_key,
                    location_toast_cache_key,
                    clock_saver_cache_key,
                    clock_saver_peek_cache_key,
                    startup_wm_cache_key,
                    paused_row_cache_key,
                    receiver_overlay_skip_sig,
                    theater_dim_key,
                    mic_viz_cache_key,
                    tmdb_x_cache_key,
                    tmdb_flag_badge_cache_key,
                )
            ):
                _schedule_next_render()
                return

            if _PIGEON_EXT:
                shown = _compose_shown_frame(
                    last_frame if not _backdrop_active else None, b_scene
                )
                shown = _blend_view_four_debug(shown)
                if (
                    lerp_bgr_red_monochrome is not None
                    and _effective_display_view() != DisplayView.FOUR
                ):
                    sm = max(0.0, min(1.0, _compose_idle_strength_holder[0]))
                    if sm > 1e-6:
                        shown = lerp_bgr_red_monochrome(shown, sm)
            else:
                shown = _apply_brightness(scaled_display, b_scene)
            if tmdb_x_animating:
                _blend_tmdb_quality_toggle_overlay(
                    shown,
                    color_bgr=_tmdb_x_bgr,
                    alpha=_tmdb_x_alpha,
                    caption=_tmdb_x_caption,
                )
            if tmdb_flag_badge_on:
                _blend_tmdb_quality_flag_badge(shown)
            _update_label_photo_from_bgr(label, shown, label_live_photo)

            if (
                not playing
                and not brightness_animating
                and not idle_dim_animating
                and not location_toast_animating
                and not tmdb_x_animating
            ):
                skip_cache = (
                    scaled_version,
                    b_key,
                    int(dev_phase),
                    int(display_view_holder[0]),
                    int(_effective_display_view()),
                    int(view_one_layout_holder[0]),
                    int(view_four_subview_holder[0]),
                    tick_key,
                    display_dims[0],
                    display_dims[1],
                    1 if _backdrop_active else 0,
                    idle_cache_key,
                    location_toast_cache_key,
                    clock_saver_cache_key,
                    clock_saver_peek_cache_key,
                    startup_wm_cache_key,
                    paused_row_cache_key,
                    receiver_overlay_skip_sig,
                    theater_dim_key,
                    mic_viz_cache_key,
                    tmdb_x_cache_key,
                    tmdb_flag_badge_cache_key,
                )
            else:
                skip_cache = None

            _schedule_next_render()

        def _invoke_render_after() -> None:
            _render_after_id[0] = None
            render_once()

        def _schedule_render_oneshot(delay_ms: int) -> None:
            if _render_after_id[0] is not None:
                try:
                    root.after_cancel(_render_after_id[0])
                except tk.TclError:
                    pass
            _render_after_id[0] = root.after(max(1, int(delay_ms)), _invoke_render_after)

        def _receiver_poll_tick() -> None:
            root.after(RECEIVER_POLL_MS, _receiver_poll_tick)
            if not _PIGEON_EXT or playback_overlay_widget is None:
                return
            if receiver_poll_busy["active"]:
                return

            host = str(receiver_http_host.get("host") or "").strip()

            def apply_overlay(incoming: str, config: str, volume: str) -> None:
                nonlocal skip_cache, last_device_interaction_mono
                receiver_poll_busy["active"] = False
                old_vol_raw = str(receiver_overlay_state.get("volume", ""))
                old_in = str(receiver_overlay_state.get("incoming", ""))
                old_cf = str(receiver_overlay_state.get("config", ""))
                new_in = str(incoming)
                new_cf = str(config)
                new_vol = str(volume)
                overlay_unchanged = (
                    old_in == new_in and old_cf == new_cf and old_vol_raw == new_vol
                )
                receiver_overlay_state["incoming"] = incoming
                receiver_overlay_state["config"] = config
                receiver_overlay_state["volume"] = volume
                if overlay_unchanged:
                    return
                last_device_interaction_mono = time.monotonic()
                if old_vol_raw != new_vol:
                    _bump_clock_saver_significant_device()
                _warm_playback_overlay_blits()
                skip_cache = None
                render_once()

            receiver_poll_busy["active"] = True

            def work() -> None:
                from pigeon.widgets.playback_overlay import (
                    _receiver_volume_display_line,
                    compose_playback_volume_widget_line,
                )

                r = None
                if host:
                    try:
                        from pigeon.receiver_denon import poll_denon_like_receiver

                        r = poll_denon_like_receiver(host, timeout=5.0)
                    except Exception:
                        r = None

                roku_line = ""
                roku_vol_pct = ""
                roku_app_name = ""
                try:
                    from pigeon.roku_ecp import (
                        fetch_roku_active_app_name,
                        fetch_roku_playback_line,
                        resolve_roku_ecp_base_url,
                        resolve_roku_ecp_base_url_for_row,
                    )

                    row_r = streaming_slot_holder[0]
                    rbase_line = ""
                    if row_r and not row_is_playback_apple_tv(row_r):
                        rbase_line = str(resolve_roku_ecp_base_url_for_row(row_r) or "").strip()
                    if not rbase_line:
                        rbase_line = str(resolve_roku_ecp_base_url() or "").strip()
                    if rbase_line:
                        rl, rv = fetch_roku_playback_line(rbase_line, timeout=3.0)
                        roku_line = rl or ""
                        roku_vol_pct = str(rv or "").strip()
                        # Keep all Roku ECP I/O off the Tk thread; this call can block on socket connect.
                        try:
                            apnm_w = fetch_roku_active_app_name(rbase_line)
                            if apnm_w:
                                roku_app_name = str(apnm_w).strip()
                        except Exception:
                            roku_app_name = ""
                except Exception:
                    roku_line = ""
                    roku_vol_pct = ""
                    roku_app_name = ""

                denon_vol_raw = ""
                if r is not None and r.ok:
                    denon_vol_raw = str(r.volume or "").strip()
                denon_vol_effective = (
                    denon_vol_raw if _receiver_volume_display_line(denon_vol_raw) else ""
                )
                merged_volume = compose_playback_volume_widget_line(
                    stream_row=streaming_slot_holder[0],
                    apple_tv_last_metadata=apple_tv_auto_state.get("last_metadata"),
                    denon_vol_effective=denon_vol_effective,
                    roku_tv_volume_percent=roku_vol_pct,
                )

                def apply() -> None:
                    rpl = receiver_panel_led_holder[0]
                    try:
                        denon_ok = r is not None and r.ok
                        if denon_ok and denon_vol_effective:
                            denon_vol_cache["effective"] = denon_vol_effective
                            denon_vol_cache["mono_usable"] = time.monotonic()
                        try:
                            from pigeon.app_state import (
                                read_current_location_id,
                                read_saved_av_receiver,
                            )
                            from pigeon.observed_capability import (
                                update_observed_capabilities_from_receiver_poll,
                            )

                            update_observed_capabilities_from_receiver_poll(
                                str(read_current_location_id() or ""),
                                read_saved_av_receiver(),
                                denon_reachable=denon_ok,
                                denon_volume_usable=bool(denon_vol_effective),
                                denon_has_incoming=bool(
                                    r is not None and str(r.incoming or "").strip()
                                ),
                                denon_has_config=bool(
                                    r is not None and str(r.config or "").strip()
                                ),
                            )
                        except Exception:
                            pass
                        _refresh_observed_pairing_led_rows()
                        if r is not None and r.ok:
                            receiver_telnet_debug_holder[0] = dict(
                                getattr(r, "telnet_debug", {}) or {}
                            )
                            apply_overlay(r.incoming, r.config, merged_volume)
                            if rpl is not None:
                                _paint_boolean_led(rpl, True)
                        elif merged_volume:
                            receiver_telnet_debug_holder[0] = {}
                            apply_overlay("", "", merged_volume)
                            if rpl is not None:
                                _paint_boolean_led(rpl, False)
                        else:
                            receiver_telnet_debug_holder[0] = {}
                            apply_overlay("", "", "")
                            if rpl is not None:
                                _paint_boolean_led(rpl, False)
                        if roku_app_name:
                            _sync_streaming_badge_from_playback_sources(
                                None,
                                roku_app_name=roku_app_name,
                            )
                    except tk.TclError:
                        pass

                root.after(0, apply)

            threading.Thread(target=work, daemon=True).start()

        shell.bind("<Configure>", _on_shell_configure)

        render_once()
        root.after(600, _receiver_poll_tick)

        if _PIGEON_EXT:
            tk.Widget.pack = _tk_pack_orig  # type: ignore[method-assign]
            tk.Widget.grid = _tk_grid_orig  # type: ignore[method-assign]
            tk.Widget.place = _tk_place_orig  # type: ignore[method-assign]
            try:
                root.update()
            except tk.TclError:
                pass
        bootstrap_done[0] = True
        _try_remove_splash_overlay()

    if _PIGEON_EXT:
        # Paint at least one splash frame before heavy bootstrap (pack/grid pump interleaves updates).
        root.after_idle(splash_tick)
        root.after(1, bootstrap)
    else:
        root.after(1, bootstrap)
    try:
        root.mainloop()
    finally:
        if cap is not None:
            cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
