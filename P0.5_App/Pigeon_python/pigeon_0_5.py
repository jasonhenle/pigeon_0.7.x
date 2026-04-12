import argparse
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
import tkinter.scrolledtext as scrolledtext
import tkinter.simpledialog as simpledialog

# 5×2 clock: row 1, cols 11–15; right edge meets col 15 (cell left of the 2-wide app badge at cols 16–17).
CLOCK_ANCHOR_ROW = 1
CLOCK_ANCHOR_COL = 11
# Status bar: one black pill cols 3–17 (row 7), bar-shaped mask hole + translucent bar; rows 6–8 gradient.
# Remaining label spans cols 16–17 (was 17–18) so the right edge has one extra column of margin.
TRT_DISPLAY_ROW = 7
TRT_PLAYED_COL = 3  # TRTPlayed (elapsed)
TRT_PLAYED_TEXT = "00:00:00"
TRT_REMAINING_COL = 16  # TRTRemaining (countdown); 2-wide → ends at col 17 (col 19 = breathing room)
TRT_REMAINING_TEXT = "01:00:00"
TRT_LABEL_SPAN_W = 2
TRT_LABEL_SPAN_H = 1
# Apple TV auto-poll; TRT labels on a steady ~1 Hz metronome (see _playback_ui_tick).
APPLE_TV_POLL_MS = 3000
RECEIVER_POLL_MS = 2500
# Receiver volume line: ramp to large type, hold, then ease back (see _tick_volume_text_boost_anim).
VOLUME_TEXT_BOOST_ANIM_UP_S = 0.085
VOLUME_TEXT_BOOST_ANIM_DOWN_S = 0.095
VOLUME_TEXT_BOOST_HOLD_S = 2.0
# Smooth volume sizing: tick + ``VOLUME_TEXT_BOOST_SIG_STEPS`` (playback_overlay) for cache/skip keys.
VOLUME_TEXT_BOOST_TICK_MS = 22
PLAYBACK_UI_TICK_MS = 1000  # fallback first delay only; actual spacing uses monotonic deadlines
# Title logo: centered on row 8 (5-wide in 19 cols → start col 8). Service badge: row 1, right edge col 17.
TMDB_LOGO_ANCHOR_ROW = 8
TMDB_LOGO_ANCHOR_COL = 8
TMDB_LOGO_SPAN_W = 5
TMDB_LOGO_SPAN_H = 1
TMDB_LOGO_FIT_SCALE = 0.88
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

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
from pigeon.stage_background import bgr_to_tk_hex, get_stage_bgr, set_stage_bgr

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
        scale_height_and_center_crop,
    )
    from pigeon.design import DESIGN_H, DESIGN_W, playback_lower_gradient_bgra, rect_for_span_at_cell
    from pigeon.overlay import blend_overlay_bgr, build_stage_overlay_source_bgra
    from pigeon.widgets.clock_calendar import ClockCalendarWidget
    from pigeon.widgets.location_toast import (
        LOCATION_TOAST_FADE_S,
        LOCATION_TOAST_FULL_S,
        location_toast_patch_bgra,
    )
    from pigeon.widgets.logo_tmdb import TmdbLogoWidget
    from pigeon.widgets.status_bar import StatusBarWidget
    from pigeon.widgets.clock_saver import clock_saver_composite_bgra
    from pigeon.widgets.playback_overlay import (
        PlaybackOverlayWidget,
        VOLUME_TEXT_BOOST_SIG_STEPS,
        pigeon_wordmark_design_patch,
    )
    from pigeon.widgets.poster_art import prepare_default_poster_at_startup

    _PIGEON_EXT = True
except ImportError:
    alpha_blend_bgra_over_bgr = None  # type: ignore[misc, assignment]
    lerp_bgr_red_monochrome = None  # type: ignore[misc, assignment]
    scale_bgra_rgb = None  # type: ignore[misc, assignment]
    scale_height_and_center_crop = None  # type: ignore[misc, assignment]
    rect_for_span_at_cell = None  # type: ignore[misc, assignment]
    playback_lower_gradient_bgra = None  # type: ignore[misc, assignment]
    DESIGN_W = DESIGN_H = 0
    blend_overlay_bgr = None  # type: ignore[misc, assignment]
    build_stage_overlay_source_bgra = None  # type: ignore[misc, assignment]
    prepare_default_poster_at_startup = None  # type: ignore[misc, assignment]
    ClockCalendarWidget = None  # type: ignore[misc, assignment]
    TmdbLogoWidget = None  # type: ignore[misc, assignment]
    StatusBarWidget = None  # type: ignore[misc, assignment]
    PlaybackOverlayWidget = None  # type: ignore[misc, assignment]
    VOLUME_TEXT_BOOST_SIG_STEPS = 400  # type: ignore[misc, assignment]
    clock_saver_composite_bgra = None  # type: ignore[misc, assignment]
    pigeon_wordmark_design_patch = None  # type: ignore[misc, assignment]
    LOCATION_TOAST_FULL_S = 15.0
    LOCATION_TOAST_FADE_S = 2.0
    location_toast_patch_bgra = None  # type: ignore[misc, assignment]
    _PIGEON_EXT = False


WINDOW_W = 800
WINDOW_H = 400
# App-logo backdrop when TMDb has no art: letterbox canvas is at most this fraction of the live window.
APP_LOGO_FALLBACK_MAX_RESOLUTION_FRACTION = 0.9
# Reserved strip at bottom when developer mode is on (must not sit under the full-bleed video label).
OVERLAY_HUD_H = 52


class DevPhase(IntEnum):
    OFF = 0
    GRID = 1
    SETTINGS = 2


# Compositing (video + widget blits) above this width uses a smaller buffer, then upscales once for Tk.
# Keeps playback smooth when the window is dragged very large.
MAX_FAST_COMPOSITE_W = 1280

# TMDb static backdrop scene (not paused-video dim 0.3).
BACKDROP_BRIGHTNESS = 0.8
# Static landing logo (no video): full brightness; old 0.3 “paused video” level hid the art.
LANDING_DISPLAY_BRIGHTNESS = 1.0
LANDING_DIM_BRIGHTNESS = 0.78  # Space-bar pulse “off” — still readable vs old 0.3
# Faint ``pigeon`` wordmark on the landing plate: hide after this from UI bootstrap (then backdrop if saved, else black).
STARTUP_PIGEON_WORDMARK_MAX_S = 5.0

# Theater UI: red luma-mono only when neither Apple TV nor Pigeon UI shows activity this long.
# ATV side: poll metadata deltas (see _update_atv_interaction_from_poll_metadata).
# Pigeon side: mouse / keyboard / settings scroll (see _bump_pigeon_user_activity).
# For ATV, 0 = never bumped → treated as idle until the first remote-driven signal (still needs Pigeon idle).
THEATER_IDLE_DIM_AFTER_S = 45.0
# Ease in/out between full color and red luma-mono when idle-dim target changes.
ATV_IDLE_MONO_ANIM_S = 2.0
# Idle “clock saver”: hide status bar / small clock / pills; keep faint pigeon wordmark. Dismiss on Pigeon or ATV activity.
# Also turns on **immediately** when a Player is selected but content is not detected (see ``_clock_saver_active``).
CLOCK_SAVER_AFTER_S = 240.0
# Saver text opacity when idle; tap while saver is up briefly uses 1.0 (see clock_saver_peek_until_mono).
CLOCK_SAVER_DIM_OPACITY = 0.3
CLOCK_SAVER_PEEK_S = 2.5
# With a TMDb backdrop visible: fade backdrop to black under the clock after this idle span on the saver.
CLOCK_SAVER_BACKDROP_BLACK_AFTER_S = 60.0
CLOCK_SAVER_BACKDROP_BLACK_FADE_S = 2.5

HOTKEY_BINDTAG = "Pigeon0_5_hotkeys"

def _paint_boolean_led(canvas: tk.Canvas, ok: bool | None) -> None:
    """Single lamp: green ok, amber when ok is None (degraded), red when False."""
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

        resized = cv2.resize(frame_bgr, (scaled_w, scaled_h), interpolation=cv2.INTER_LINEAR)

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


def _load_persisted_scene_enabled(default: bool = True) -> bool:
    v = read_app_state().get("scene_enabled")
    if isinstance(v, bool):
        return v
    return default


def _save_persisted_scene_enabled(enabled: bool) -> None:
    write_app_state(scene_enabled=enabled)


def _format_hmmss(seconds_value: float | int | None) -> str:
    try:
        total_seconds = max(0, int(float(seconds_value or 0)))
    except (TypeError, ValueError):
        total_seconds = 0
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    # Zero-pad hours when < 100 so TRT strings stay 8 chars (fixed-width timecode layout).
    if hours < 100:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def main() -> int:
    sys.stderr.write(f"pigeon: running script {os.path.abspath(__file__)}\n")
    sys.stderr.flush()

    parser = argparse.ArgumentParser(prog="Pigeon 0.5", add_help=True)
    parser.parse_args()

    cap: cv2.VideoCapture | None = None

    root = tk.Tk()
    root.title("")
    root.geometry(f"{WINDOW_W}x{WINDOW_H}")
    root.minsize(400, 200)
    root.resizable(True, True)
    try:
        root.wm_aspect(2, 1, 2, 1)
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

    loading = tk.Label(
        shell,
        text="Starting Pigeon…\n\n"
        "Tab cycles developer mode: off → grid overlay → settings → off. "
        "Return opens the command bar while in developer mode. "
        "Esc closes the bar or quits. F10 / double-click toggles the display. "
        "Space = play/pause on the selected Player (Apple TV / Roku) when set; else TMDb backdrop "
        "+ logo when loaded; else landing brightness pulse.",
        justify="center",
        fg="#ddd",
        bg="#111",
        wraplength=WINDOW_W - 40,
    )
    loading.pack(expand=True, fill="both")
    root.update_idletasks()
    root.update()

    paused_interval_ms = 33

    def bootstrap() -> None:
        nonlocal cap

        cap = None

        loading.destroy()

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
        video_area = tk.Frame(shell, bg="#000")
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
        settings_footer_debug_holder: list[tk.Button | None] = [None]
        settings_footer_reset_holder: list[tk.Button | None] = [None]
        apple_tv_busy = {"active": False}
        apple_tv_auto_state: dict[str, object] = {
            "running": False,
            "content_key": None,
            "tmdb_key": None,
            "query": None,
            "prefer": "auto",
            "last_metadata": None,
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
        def _atv_metadata_is_content_idle(metadata: dict[str, object]) -> bool:
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

        # HUD is placed on top of the bottom of the shell (does not resize the video — same 800×400 image).
        hud_bar = tk.Frame(shell, height=OVERLAY_HUD_H, bg="#243949")
        hud_bar.pack_propagate(False)
        hud = tk.Label(
            hud_bar,
            text="",
            fg="#f2f2f2",
            bg="#243949",
            font=("Helvetica", 11),
            wraplength=WINDOW_W - 24,
            justify="center",
        )
        hud.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        fps_sched = _default_render_fps()
        display_dims = [WINDOW_W, WINDOW_H]
        fit_holder = [SceneFit(target_w=WINDOW_W, target_h=WINDOW_H)]

        if _PIGEON_EXT:
            from pigeon.design import DESIGN_W as _DESIGN_W_L, DESIGN_H as _DESIGN_H_L

            _land_w, _land_h = int(_DESIGN_W_L), int(_DESIGN_H_L)
        else:
            _land_w, _land_h = WINDOW_W, WINDOW_H
        # No PNG on the landing plate — the ``pigeon`` wordmark is drawn in the playback overlay only.
        landing_scene_design_bgr = _build_landing_design_bgr(_land_w, _land_h, None)

        def _disp_fit() -> SceneFit:
            return fit_holder[0]

        def _black_screen_bgr() -> np.ndarray:
            sb, sg, sr = get_stage_bgr()
            out = np.empty((display_dims[1], display_dims[0], 3), dtype=np.uint8)
            out[:] = (sb, sg, sr)
            return out

        frame_interval_ms = max(1, int(round(1000.0 / fps_sched)))

        playing = False
        last_frame: np.ndarray | None = landing_scene_design_bgr
        brightness_current = LANDING_DISPLAY_BRIGHTNESS
        brightness_from = LANDING_DISPLAY_BRIGHTNESS
        brightness_target = LANDING_DISPLAY_BRIGHTNESS
        brightness_t0 = time.monotonic()
        brightness_duration_s = 3.0
        brightness_duration_up_s = 1.0
        brightness_duration_down_s = 1.0

        last_atv_interaction_mono = 0.0
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

        def _last_unified_activity_mono() -> float:
            """Most recent Pigeon UI or Apple TV remote–like signal (for clock saver idle)."""
            pigeon = last_pigeon_user_activity_mono[0]
            atv = last_atv_interaction_mono
            if atv <= 0.0:
                return pigeon
            return max(pigeon, atv)

        def _clock_saver_immediate_for_no_content() -> bool:
            """
            True when a Player (Apple TV) is selected, we are past the first poll, and the content LED
            would be off — same rules as ``_content_indicator_ok`` (no usable query / failed poll).
            """
            if not _PIGEON_EXT:
                return False
            if not current_apple_tv.get("identifier"):
                return False
            if apple_tv_busy["active"]:
                return False
            if apple_tv_dashboard_track.get("last_poll_ok") is None:
                return False
            return not _content_indicator_ok()

        def _clock_saver_active(now: float) -> bool:
            if clock_saver_composite_bgra is None:
                return False
            if dev_phase != DevPhase.OFF:
                return False
            if not scene_enabled:
                return False
            if _clock_saver_immediate_for_no_content():
                return True
            return (now - _last_unified_activity_mono()) >= CLOCK_SAVER_AFTER_S

        def _clock_saver_layer_opacity(now: float) -> float:
            if now < clock_saver_peek_until_mono[0]:
                return 1.0
            return CLOCK_SAVER_DIM_OPACITY

        def _clock_saver_backdrop_blackout_mix(now: float) -> float:
            if not use_backdrop_scene or backdrop_master_bgr is None:
                return 0.0
            if not _clock_saver_active(now):
                return 0.0
            t0 = clock_saver_bd_enter_mono[0]
            if t0 is None:
                return 0.0
            elapsed = now - float(t0)
            if elapsed < CLOCK_SAVER_BACKDROP_BLACK_AFTER_S:
                return 0.0
            u = elapsed - CLOCK_SAVER_BACKDROP_BLACK_AFTER_S
            if u >= CLOCK_SAVER_BACKDROP_BLACK_FADE_S:
                return 1.0
            return u / CLOCK_SAVER_BACKDROP_BLACK_FADE_S

        idle_dim_anim_strength = 0.0
        _idle_dim_anim_goal = 0.0
        _idle_dim_anim_from = 0.0
        _idle_dim_anim_t0 = time.monotonic()

        def _update_idle_dim_strength(now: float) -> float:
            """0 = full color, 1 = red luma-mono; eases in/out over ATV_IDLE_MONO_ANIM_S when combined idle state changes."""
            nonlocal idle_dim_anim_strength, _idle_dim_anim_goal, _idle_dim_anim_from, _idle_dim_anim_t0
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

        skip_cache: tuple[int, float, int, int, int, int, int, int, int, int, int] | None = None
        scene_enabled = _load_persisted_scene_enabled(True)
        dev_phase = DevPhase.OFF
        # Advanced matrix: temporarily show GRID behind the dialog when opened from Settings; restore on close.
        advanced_matrix_restore_phase: list[object] = [None]
        advanced_matrix_close_skip: list[bool] = [False]
        location_toast_state: dict[str, object] = {"active": False, "text": "", "t0": 0.0}
        prev_dev_phase_for_location_toast: list[DevPhase] = [DevPhase.OFF]
        clock_saver_peek_until_mono: list[float] = [0.0]
        clock_saver_bd_enter_mono: list[float | None] = [None]
        black_photo: ImageTk.PhotoImage | None = None
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
            ClockCalendarWidget(anchor_row=CLOCK_ANCHOR_ROW, anchor_col=CLOCK_ANCHOR_COL)
            if _PIGEON_EXT and ClockCalendarWidget is not None
            else None
        )
        tmdb_logo_widget = (
            TmdbLogoWidget(
                anchor_row=TMDB_LOGO_ANCHOR_ROW,
                anchor_col=TMDB_LOGO_ANCHOR_COL,
                span_wide=TMDB_LOGO_SPAN_W,
                span_tall=TMDB_LOGO_SPAN_H,
                fit_scale=TMDB_LOGO_FIT_SCALE,
            )
            if _PIGEON_EXT and TmdbLogoWidget is not None
            else None
        )
        active_tmdb_title_key: str | None = None
        active_tmdb_display_title: str | None = None
        status_bar_widget = None
        if _PIGEON_EXT and StatusBarWidget is not None:
            status_bar_widget = StatusBarWidget(
                assets_dir=Path(_SCRIPT_DIR) / "pigeonAssets",
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
        streaming_badge_state: dict[str, object] = {
            "show": False,
            "filename": "",
            "label": "",
        }
        playback_overlay_flags: dict[str, bool] = {
            "hide_wordmark_for_artwork": False,
            "show_paused_row": False,
        }
        playback_overlay_widget = None
        if _PIGEON_EXT and PlaybackOverlayWidget is not None:
            playback_overlay_widget = PlaybackOverlayWidget(
                assets_dir=Path(_SCRIPT_DIR) / "pigeonAssets",
                receiver_state=receiver_overlay_state,
                service_badge=streaming_badge_state,
                overlay_flags=playback_overlay_flags,
            )

        _volume_boost_strength: list[float] = [0.0]
        _volume_hold_until_mono: list[float] = [0.0]
        _volume_anim_last_mono: list[float] = [time.monotonic()]
        _volume_boost_cache_k: list[int] = [-1]

        def _volume_effective_display(raw: str) -> str:
            from pigeon.widgets.playback_overlay import _receiver_volume_display_line

            return str(_receiver_volume_display_line(raw) or "").strip()

        def _on_receiver_volume_display_changed(old_raw: str, new_raw: str) -> None:
            od = _volume_effective_display(old_raw)
            nd = _volume_effective_display(new_raw)
            if od == nd:
                return
            if not nd:
                _volume_hold_until_mono[0] = 0.0
                return
            _volume_hold_until_mono[0] = time.monotonic() + VOLUME_TEXT_BOOST_HOLD_S
            _volume_boost_cache_k[0] = -1

        def _volume_boost_smoothed() -> float:
            return max(0.0, min(1.0, float(_volume_boost_strength[0])))

        def _volume_boost_cache_key() -> int:
            n = max(1, int(VOLUME_TEXT_BOOST_SIG_STEPS))
            return max(0, min(n, int(round(_volume_boost_smoothed() * n))))

        def _apply_volume_boost_to_widget() -> None:
            if playback_overlay_widget is None:
                return
            playback_overlay_widget.volume_text_boost_strength = _volume_boost_smoothed()

        def _maybe_rewarm_volume_overlay_for_smooth_boost() -> bool:
            """Rebuild playback overlay when boost moved to a new cache step; widget always holds smooth [0,1]."""
            if playback_overlay_widget is None:
                return False
            _apply_volume_boost_to_widget()
            k = _volume_boost_cache_key()
            if k == _volume_boost_cache_k[0]:
                return False
            _volume_boost_cache_k[0] = k
            _warm_playback_overlay_blits()
            return True

        _pigeon_ui_started_mono = time.monotonic()
        _startup_splash_complete: list[bool] = [False]

        clock_patch_bgra: np.ndarray | None = None
        tmdb_logo_patch_bgra: np.ndarray | None = None
        status_bar_blits: list = []
        playback_overlay_blits: list = []
        # [unix_sec, status_bar accent BGR or None] — clock patch invalidation.
        _clock_patch_sig: list = [-1, None]

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

        def _warm_playback_overlay_blits() -> None:
            nonlocal playback_overlay_blits
            if playback_overlay_widget is None:
                playback_overlay_blits = []
                return
            now_wm = time.monotonic()
            playback_overlay_flags["hide_wordmark_for_artwork"] = bool(
                use_backdrop_scene
            ) or (now_wm - _pigeon_ui_started_mono) >= STARTUP_PIGEON_WORDMARK_MAX_S
            playback_overlay_flags["show_paused_row"] = _show_paused_row_overlay()
            playback_overlay_blits = list(playback_overlay_widget.design_blits())

        def _warm_tmdb_logo_patch() -> None:
            nonlocal tmdb_logo_patch_bgra
            if tmdb_logo_widget is None or not active_tmdb_title_key:
                tmdb_logo_patch_bgra = None
                return
            tmdb_logo_patch_bgra = tmdb_logo_widget.bgra_patch_for_title(
                active_tmdb_title_key,
                display_title=active_tmdb_display_title,
            ).copy()

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

        _playback_overlay_fast_sig: list[tuple[bool, bool] | None] = [None]

        def compose_display_fast_no_grid(
            frame_bgr: np.ndarray | None,
            brightness: float,
            *,
            frame_is_display_sized: bool = False,
        ) -> np.ndarray:
            """Video at display size + poster/clock blits (no full design canvas). Used when developer grid is off."""
            assert _PIGEON_EXT
            now_wm = time.monotonic()
            hide_wm = bool(use_backdrop_scene) or (
                now_wm - _pigeon_ui_started_mono
            ) >= STARTUP_PIGEON_WORDMARK_MAX_S
            vol_k = 0
            if playback_overlay_widget is not None:
                n = max(1, int(VOLUME_TEXT_BOOST_SIG_STEPS))
                vb = max(
                    0.0,
                    min(1.0, float(playback_overlay_widget.volume_text_boost_strength)),
                )
                vol_k = max(0, min(n, int(round(vb * n))))
            fast_sig = (hide_wm, _show_paused_row_overlay(), vol_k)
            if playback_overlay_widget is not None and _playback_overlay_fast_sig[0] != fast_sig:
                _playback_overlay_fast_sig[0] = fast_sig
                _warm_playback_overlay_blits()
            dw, dh = display_dims[0], display_dims[1]
            cap_w = min(dw, MAX_FAST_COMPOSITE_W)
            cap_h = max(1, int(round(dh * (cap_w / float(dw))))) if dw > 0 else dh
            use_cap = cap_w < dw

            if frame_bgr is None or frame_bgr.size == 0:
                sb, sg, sr = get_stage_bgr()
                base = np.empty((cap_h, cap_w, 3), dtype=np.uint8)
                base[:] = (sb, sg, sr)
            else:
                lit = _apply_brightness(frame_bgr, brightness)
                if frame_is_display_sized:
                    base = lit
                else:
                    fit = SceneFit(target_w=cap_w, target_h=cap_h) if use_cap else _disp_fit()
                    base = fit.scale_and_crop(lit)
            now_cs = time.monotonic()
            cs = _clock_saver_active(now_cs)
            if cs and use_backdrop_scene and backdrop_master_bgr is not None:
                if clock_saver_bd_enter_mono[0] is None:
                    clock_saver_bd_enter_mono[0] = now_cs
            elif not cs or not use_backdrop_scene:
                clock_saver_bd_enter_mono[0] = None
            mx_bd = _clock_saver_backdrop_blackout_mix(now_cs)
            if mx_bd > 1e-5:
                base = (base.astype(np.float32) * (1.0 - mx_bd)).astype(np.uint8)
            if cs:
                if alpha_blend_bgra_over_bgr is not None:
                    acc_cs = (
                        tuple(status_bar_widget.accent_bgr)
                        if status_bar_widget is not None
                        else None
                    )
                    (time_bgra, t_rect), (date_bgra, d_rect) = clock_saver_composite_bgra(
                        shadow_bgr=acc_cs,
                        layer_opacity=_clock_saver_layer_opacity(now_cs),
                        clock_anchor_row=CLOCK_ANCHOR_ROW,
                        clock_anchor_col=CLOCK_ANCHOR_COL,
                    )
                    for cs_bgra, (sx, sy, sw, sh) in (
                        (time_bgra, t_rect),
                        (date_bgra, d_rect),
                    ):
                        x, y, rw, rh = _design_rect_to_target(sx, sy, sw, sh, cap_w, cap_h)
                        patch = cv2.resize(cs_bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
                        sub = base[y : y + rh, x : x + rw]
                        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
            else:
                if (
                    playback_lower_gradient_bgra is not None
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    gx, gy, gw, gh, grad_bgra = playback_lower_gradient_bgra()
                    x, y, rw, rh = _design_rect_to_target(gx, gy, gw, gh, cap_w, cap_h)
                    patch = cv2.resize(grad_bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
                    sub = base[y : y + rh, x : x + rw]
                    sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                _refresh_clock_patch_bgra()
                if (
                    clock_patch_bgra is not None
                    and clock_widget is not None
                    and rect_for_span_at_cell is not None
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    sw, sh = clock_widget.grid_span
                    ar, ac = clock_widget.grid_anchor
                    wx, wy, ww, wh = rect_for_span_at_cell(sw, sh, row_1based=ar, col_1based=ac)
                    x, y, rw, rh = _design_rect_to_target(wx, wy, ww, wh, cap_w, cap_h)
                    patch = cv2.resize(clock_patch_bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
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
                        )
                        if patch_lt is not None:
                            x, y, rw, rh = _design_rect_to_target(lwx, lwy, lww, lwh, cap_w, cap_h)
                            patch = cv2.resize(patch_lt, (rw, rh), interpolation=cv2.INTER_LINEAR)
                            sub = base[y : y + rh, x : x + rw]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if status_bar_blits and alpha_blend_bgra_over_bgr is not None:
                    for sb in status_bar_blits:
                        x0, y0, ww, wh = int(sb.x), int(sb.y), int(sb.w), int(sb.h)
                        x, y, rw, rh = _design_rect_to_target(x0, y0, ww, wh, cap_w, cap_h)
                        patch = cv2.resize(sb.bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
                        sub = base[y : y + rh, x : x + rw]
                        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if playback_overlay_blits and alpha_blend_bgra_over_bgr is not None:
                    for pb in playback_overlay_blits:
                        x0, y0, ww, wh = int(pb.x), int(pb.y), int(pb.w), int(pb.h)
                        x, y, rw, rh = _design_rect_to_target(x0, y0, ww, wh, cap_w, cap_h)
                        patch = cv2.resize(pb.bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
                        sub = base[y : y + rh, x : x + rw]
                        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
                if (
                    tmdb_logo_patch_bgra is not None
                    and tmdb_logo_widget is not None
                    and alpha_blend_bgra_over_bgr is not None
                ):
                    wx, wy, ww, wh = tmdb_logo_widget.design_rect()
                    x, y, rw, rh = _design_rect_to_target(wx, wy, ww, wh, cap_w, cap_h)
                    patch = cv2.resize(tmdb_logo_patch_bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
                    sub = base[y : y + rh, x : x + rw]
                    sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
            if use_cap:
                return cv2.resize(base, (dw, dh), interpolation=cv2.INTER_LINEAR)
            return base

        def compose_display_from_source(
            frame_bgr: np.ndarray | None,
            brightness: float,
            *,
            show_grid: bool,
            frame_is_design_sized: bool = False,
        ) -> np.ndarray:
            """
            Build WINDOW_W×WINDOW_H output: scale **source** video to design, draw widgets, optionally grid,
            then scale down. Using the raw frame avoids letterboxing an already 800×400 image (which shifted
            the grid/poster and cropped them on the left).
            """
            assert _PIGEON_EXT
            assert scale_height_and_center_crop is not None
            assert blend_overlay_bgr is not None
            assert build_stage_overlay_source_bgra is not None
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
            now_cs = time.monotonic()
            cs = _clock_saver_active(now_cs) and not show_grid
            if cs and use_backdrop_scene:
                if clock_saver_bd_enter_mono[0] is None:
                    clock_saver_bd_enter_mono[0] = now_cs
            elif not cs or not use_backdrop_scene:
                clock_saver_bd_enter_mono[0] = None
            mx_bd = _clock_saver_backdrop_blackout_mix(now_cs)
            if mx_bd > 1e-5:
                canvas = (canvas.astype(np.float32) * (1.0 - mx_bd)).astype(np.uint8)
            if cs:
                if alpha_blend_bgra_over_bgr is not None:
                    acc_cs = (
                        tuple(status_bar_widget.accent_bgr)
                        if status_bar_widget is not None
                        else None
                    )
                    (time_bgra, t_rect), (date_bgra, d_rect) = clock_saver_composite_bgra(
                        shadow_bgr=acc_cs,
                        layer_opacity=_clock_saver_layer_opacity(now_cs),
                        clock_anchor_row=CLOCK_ANCHOR_ROW,
                        clock_anchor_col=CLOCK_ANCHOR_COL,
                    )
                    for cs_bgra, (sx, sy, sw, sh) in (
                        (time_bgra, t_rect),
                        (date_bgra, d_rect),
                    ):
                        roi2 = canvas[sy : sy + sh, sx : sx + sw]
                        roi2[:] = alpha_blend_bgra_over_bgr(roi2, cs_bgra)
            else:
                if playback_lower_gradient_bgra is not None and alpha_blend_bgra_over_bgr is not None:
                    gx, gy, gw, gh, grad_bgra = playback_lower_gradient_bgra()
                    sub = canvas[gy : gy + gh, gx : gx + gw]
                    sub[:] = alpha_blend_bgra_over_bgr(sub, grad_bgra)
                if clock_widget is not None:
                    clock_widget.render(canvas)
                if (
                    not show_grid
                    and dev_phase == DevPhase.OFF
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
                        )
                        if patch_lt is not None:
                            sub = canvas[lwy : lwy + lwh, lwx : lwx + lww]
                            sub[:] = alpha_blend_bgra_over_bgr(sub, patch_lt)
                if status_bar_widget is not None:
                    status_bar_widget.render(canvas)
                if playback_overlay_widget is not None:
                    now_wm = time.monotonic()
                    playback_overlay_flags["hide_wordmark_for_artwork"] = bool(
                        use_backdrop_scene
                    ) or (now_wm - _pigeon_ui_started_mono) >= STARTUP_PIGEON_WORDMARK_MAX_S
                    playback_overlay_flags["show_paused_row"] = _show_paused_row_overlay()
                    playback_overlay_widget.render(canvas)
                if tmdb_logo_widget is not None:
                    tmdb_logo_widget.render(
                        canvas,
                        title_key_str=active_tmdb_title_key,
                        display_title=active_tmdb_display_title,
                    )
            if show_grid:
                ov = build_stage_overlay_source_bgra()
                canvas = blend_overlay_bgr(canvas, ov)
            tw, th = display_dims[0], display_dims[1]
            if tw > MAX_FAST_COMPOSITE_W:
                cw = MAX_FAST_COMPOSITE_W
                ch = max(1, int(round(th * (cw / float(tw)))))
                out = scale_height_and_center_crop(canvas, cw, ch)
                return cv2.resize(out, (tw, th), interpolation=cv2.INTER_LINEAR)
            return scale_height_and_center_crop(canvas, tw, th)

        def _compose_shown_frame(frame_bgr: np.ndarray | None, brightness: float) -> np.ndarray:
            if use_backdrop_scene and backdrop_master_bgr is not None:
                from pigeon.image_ui_protocol import build_backdrop_design_layer_bgr

                if not _PIGEON_EXT:
                    # Legacy path: use backdrop-only display if extension isn't available.
                    bd = build_backdrop_design_layer_bgr(
                        backdrop_master_bgr, app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit
                    )
                    return compose_display_from_source(bd, brightness, show_grid=False, frame_is_design_sized=True)
                bd = build_backdrop_design_layer_bgr(
                    backdrop_master_bgr, app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit
                )
                return compose_display_from_source(
                    bd,
                    brightness,
                    show_grid=(dev_phase == DevPhase.GRID),
                    frame_is_design_sized=True,
                )

            if not _PIGEON_EXT:
                if frame_bgr is None or frame_bgr.size == 0:
                    return _black_screen_bgr()
                lit = _apply_brightness(frame_bgr, brightness)
                dw, dh = display_dims[0], display_dims[1]
                cw = min(dw, MAX_FAST_COMPOSITE_W)
                ch = max(1, int(round(dh * (cw / float(dw))))) if dw > 0 else dh
                small = SceneFit(target_w=cw, target_h=ch).scale_and_crop(lit)
                if cw < dw:
                    return cv2.resize(small, (dw, dh), interpolation=cv2.INTER_LINEAR)
                return small
            if dev_phase == DevPhase.GRID and _PIGEON_EXT:
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
            ui = _ui_scale()
            hud.configure(wraplength=max(80, dw - 24), font=("Helvetica", max(8, int(11 * ui))))
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
            hud_h = max(28, int(OVERLAY_HUD_H * ui))
            bar_h = max(24, int(32 * ui))
            # Settings has no bottom HUD strip; grid keeps it for shortcuts help.
            if dev_phase == DevPhase.OFF or dev_phase == DevPhase.SETTINGS:
                yb = dh - bar_h - int(4 * ui)
            else:
                yb = dh - hud_h - bar_h - int(4 * ui)
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

        def _start_location_toast() -> None:
            nonlocal skip_cache
            if not _PIGEON_EXT:
                return
            st = location_toast_state
            st["text"] = _current_location_display_name()
            st["active"] = True
            st["t0"] = time.monotonic()
            skip_cache = None

        def sync_developer_chrome() -> None:
            was_phase = prev_dev_phase_for_location_toast[0]
            _apply_dev_phase_widgets()
            _layout_chrome()
            dw, dh = display_dims[0], display_dims[1]
            ui = _ui_scale()
            hud_h = max(28, int(OVERLAY_HUD_H * ui))
            if dev_phase == DevPhase.GRID:
                root.title("Pigeon 0.5 — Developer mode (grid)")
                label.configure(
                    highlightthickness=3,
                    highlightbackground="#0a84ff",
                    highlightcolor="#0a84ff",
                )
                hud.configure(
                    text=(
                        "Developer mode (grid) — Tab: next | Return: command bar | S: scene toggle | "
                        "F10: landing → black → backdrop → landing (backdrop after TMDb) | "
                        "? (Shift+/): TMDb retry (cycle movie / tv / alt query / auto) | dbl-click"
                        + (
                            ""
                            if _PIGEON_EXT
                            else " | (install: run from Pigeon_python folder so `pigeon` package loads for grid+poster)"
                        )
                    ),
                )
                hud_bar.place(x=0, y=dh - hud_h, width=dw, height=hud_h)
                hud_bar.lift()
            elif dev_phase == DevPhase.SETTINGS:
                root.title("Pigeon 0.5 — Developer mode (settings)")
                try:
                    label.configure(highlightthickness=0)
                except tk.TclError:
                    pass
                hud_bar.place_forget()
            else:
                root.title("")
                label.configure(highlightthickness=0)
                hud_bar.place_forget()
                hide_command_entry()
            if dev_phase == DevPhase.SETTINGS:
                _settings_bind_wheel_globals()
                root.after_idle(_settings_update_scrollregion)
            else:
                _settings_unbind_wheel_globals()
            if command_entry_visible:
                place_command_bar()
                command_bar.lift()
            if _PIGEON_EXT and dev_phase == DevPhase.OFF and was_phase != DevPhase.OFF:
                _start_location_toast()
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
            dev_phase = DevPhase((int(dev_phase) + 1) % 3)
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
            if require_overlay and dev_phase != DevPhase.GRID:
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
                    tk_img = _bgr_to_tk_image(out_bgr)
                    label.configure(image=tk_img)
                    label.image = tk_img
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
                tk_img = _bgr_to_tk_image(shown)
                label.configure(image=tk_img)
                label.image = tk_img

        _last_overlay_mono = [0.0]
        _last_s_mono = [0.0]
        _last_f10_mono = [0.0]
        _last_tmdb_hotkey_mono = [0.0]
        _last_space_mono = [0.0]
        _last_adv_shift_tab_mono = [0.0]

        def try_cycle_dev_phase(event: tk.Event | None) -> str | None:
            if event is not None:
                if getattr(event, "keysym", "") == "ISO_Left_Tab":
                    return None
                if int(getattr(event, "state", 0)) & 0x0001:
                    return None
            now = time.monotonic()
            if now - _last_overlay_mono[0] < 0.08:
                return "break"
            _last_overlay_mono[0] = now
            cycle_dev_phase()
            return "break"

        def on_tab_key(event: tk.Event) -> str | None:
            return try_cycle_dev_phase(event)

        def on_ctrl_tab(event: tk.Event) -> str | None:
            if not (int(getattr(event, "state", 0)) & 0x0004):
                return None
            return try_cycle_dev_phase(None)

        def on_s_key(event: tk.Event) -> str | None:
            keysym = (getattr(event, "keysym", "") or "").lower()
            ch = (getattr(event, "char", "") or "").lower()
            if keysym != "s" and ch != "s":
                return None
            if dev_phase != DevPhase.GRID:
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
                )
            else:
                scaled_display = None
            scaled_version += 1
            brightness_current = brightness_from = brightness_target = BACKDROP_BRIGHTNESS
            brightness_t0 = time.monotonic()
            _warm_tmdb_logo_patch()
            _save_persisted_scene_enabled(True)
            skip_cache = None
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
            if dev_phase == DevPhase.GRID:
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
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS):
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

        def spawn_tmdb_poster_fetch(query: str, *, prefer: str = "auto") -> None:
            """TMDb search + download + poster pipeline on a worker thread."""
            from pigeon.tmdb_poster import is_degenerate_tmdb_query, refine_tmdb_search_query

            q = refine_tmdb_search_query(query.strip()) or ""
            if not q:
                return
            if is_degenerate_tmdb_query(q):
                return

            def _backdrop_from_current_app_logo() -> np.ndarray | None:
                """If TMDb has no backdrop, letterbox the app logo on black (≤90% of window W×H, never cropped)."""
                fn = str(streaming_badge_state.get("filename") or "").strip()
                if not fn:
                    return None
                p = Path(_SCRIPT_DIR) / "pigeonAssets" / fn
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

            def finish_tmdb(ok_m: bool, msg_m: str, backdrop_master: np.ndarray | None = None) -> None:
                nonlocal skip_cache, cap, scene_enabled, last_frame, scaled_display, scaled_version, playing, use_backdrop_scene, backdrop_master_bgr, saved_backdrop_master_bgr, saved_backdrop_app_logo_letterbox_fit, backdrop_app_logo_letterbox_fit, brightness_current, brightness_from, brightness_target, brightness_t0, active_tmdb_title_key, active_tmdb_display_title, tmdb_logo_patch_bgra
                sys.stderr.write(f"pigeon: tmdb → {msg_m}\n")
                sys.stderr.flush()
                if not ok_m:
                    messagebox.showerror("TMDb poster", msg_m)
                    return
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
                _warm_tmdb_logo_patch()
                bd_use = backdrop_master
                from_app_logo = False
                if bd_use is None:
                    bd_use = _backdrop_from_current_app_logo()
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
                    if status_bar_widget is not None:
                        bd_arr = np.asarray(bd_use, dtype=np.uint8)
                        if status_bar_widget.set_accent_from_backdrop_bgr(bd_arr):
                            _warm_status_bar_blits()
                            skip_cache = None
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

        def _refresh_content_indicator() -> None:
            cv = content_indicator_cv_holder[0]
            if cv is None:
                return
            try:
                _paint_boolean_led(cv, _content_indicator_ok())
            except tk.TclError:
                pass

        def _paint_pair_led(which: int, ok: bool | None) -> None:
            """None = amber (credentials on disk but playback poll failing)."""
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
            """Same semantics as ``_paint_pair_led`` (None = amber), for arbitrary canvases."""
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
                            if use_live_leds:
                                paired_ui_leds[led_key] = cv
                            else:
                                _paint_cred_led_canvas(cv, False)
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
                        tk.Label(
                            head_o,
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
                        tk.Label(
                            head_a,
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
            cred_path = Path.home() / ".pigeon_0_5" / "pyatv_credentials"
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
                            "~/.pigeon_0_5/state.json to http://TV_IP:8060\n"
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
            clk = apple_tv_playback_clock
            ds = str(metadata.get("device_state") or "")
            playing_now = "Playing" in ds
            now_m = time.monotonic()
            content_key = _content_key_from_metadata(metadata)

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

            _atv_ix_sig_ds = ds
            _atv_ix_sig_ck = ck
            _atv_ix_prev_idle = idle_now
            if pos is not None:
                _atv_ix_pos = pos
                _atv_ix_pos_mono = now
            _atv_ix_extrap_playing = "Playing" in ds

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
                show = bool(
                    (clk.get("live_mode") and clk.get("playing"))
                    or clk.get("has_sync")
                )
            if status_bar_widget.set_now_playing_chrome_visible(show):
                _warm_status_bar_blits()
                skip_cache = None

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

            assets = Path(_SCRIPT_DIR) / "pigeonAssets"
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
            streaming_badge_state["show"] = show
            streaming_badge_state["filename"] = filename
            streaming_badge_state["label"] = label
            _warm_playback_overlay_blits()
            skip_cache = None
            try:
                render_once()
            except Exception:
                pass

        def _return_to_landing_if_atv_idle(metadata: dict[str, object]) -> None:
            """When Apple TV reports no playback, drop TMDb backdrop and show the static landing page."""
            nonlocal use_backdrop_scene, backdrop_master_bgr, backdrop_app_logo_letterbox_fit, last_frame, scaled_display, scaled_version, skip_cache
            nonlocal active_tmdb_title_key, active_tmdb_display_title, tmdb_logo_patch_bgra, scene_enabled, playing
            if not _atv_metadata_is_content_idle(metadata):
                return
            apple_tv_auto_state["content_key"] = None
            apple_tv_auto_state["query"] = None
            apple_tv_auto_state["prefer"] = "auto"
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
            if tmdb_logo_widget is not None:
                tmdb_logo_widget.clear_cache()
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

                def finish() -> None:
                    apple_tv_auto_state["running"] = False
                    pyatv_ok = bool(ok_w and isinstance(metadata_w, dict))
                    md_for_status: dict[str, object] | None = metadata_w if pyatv_ok else None
                    if current_apple_tv.get("identifier"):
                        if pyatv_ok:
                            apple_tv_dashboard_track["last_poll_ok"] = True
                            apple_tv_dashboard_track["consecutive_fail"] = 0
                        else:
                            apple_tv_dashboard_track["last_poll_ok"] = False
                            apple_tv_dashboard_track["consecutive_fail"] = int(
                                apple_tv_dashboard_track.get("consecutive_fail", 0)
                            ) + 1
                            cf = int(apple_tv_dashboard_track.get("consecutive_fail", 0) or 0)
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
                    _refresh_content_indicator()
                    if metadata_w:
                        if ok_w:
                            _update_atv_interaction_from_poll_metadata(metadata_w)
                        prefer_snap = _tmdb_pref_from_metadata(metadata_w)
                        apple_tv_auto_state["last_metadata"] = {
                            "query": str(metadata_w.get("query") or "").strip(),
                            "title": str(metadata_w.get("title") or "").strip(),
                            "artist": str(metadata_w.get("artist") or "").strip(),
                            "series_name": str(metadata_w.get("series_name") or "").strip(),
                            "album": str(metadata_w.get("album") or "").strip(),
                            "media_type": str(metadata_w.get("media_type") or "").strip(),
                            "total_time": metadata_w.get("total_time"),
                            "position": metadata_w.get("position"),
                            "device_state": str(metadata_w.get("device_state") or "").strip(),
                            "inferred_prefer": prefer_snap,
                            "content_key": _content_key_from_metadata(metadata_w),
                            "app_name": str(metadata_w.get("app_name") or "").strip(),
                            "app_id": str(metadata_w.get("app_id") or "").strip(),
                        }
                        _update_status_bar_from_metadata(metadata_w)
                    md_poll = metadata_w if isinstance(metadata_w, dict) else None
                    md_act = md_poll is not None and not _atv_metadata_is_content_idle(md_poll)
                    pyatv_has_app = False
                    if md_act:
                        pyatv_has_app = bool(
                            str(md_poll.get("app_name") or "").strip()
                            or str(md_poll.get("app_id") or "").strip()
                        )
                    roku_nm: str | None = None
                    if not pyatv_has_app:
                        try:
                            from pigeon.roku_ecp import (
                                fetch_roku_active_app_name,
                                resolve_roku_ecp_base_url_for_row,
                            )

                            row0 = streaming_slot_holder[0]
                            rb = resolve_roku_ecp_base_url_for_row(row0) if row0 else ""
                            if rb:
                                t = fetch_roku_active_app_name(rb)
                                if t:
                                    roku_nm = t
                        except Exception:
                            roku_nm = None
                    _sync_streaming_badge_from_playback_sources(
                        md_poll,
                        roku_app_name=roku_nm,
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
                    if not pyatv_tmdb_eligible:
                        row0_nf = streaming_slot_holder[0]
                        if row0_nf is not None and not row_is_playback_apple_tv(row0_nf):
                            try:
                                from pigeon.roku_ecp import (
                                    fetch_roku_title_for_metadata,
                                    resolve_roku_ecp_base_url_for_row,
                                )
                                from pigeon.tmdb_poster import is_degenerate_tmdb_query

                                rb_nf = resolve_roku_ecp_base_url_for_row(row0_nf)
                                if rb_nf:
                                    r_ok, _rmsg, rtitle = fetch_roku_title_for_metadata(
                                        rb_nf, timeout=6.0
                                    )
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
                                            "app_name": str(roku_nm or ""),
                                            "app_id": "",
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
                    root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)

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

        purge_image_media_btn = tk.Button(
            content_buttons_row,
            text="Purge Image Media",
            command=on_purge_image_media,
            font=S_FONT_BTN,
            padx=8,
            pady=4,
        )
        purge_image_media_btn.pack(side=tk.LEFT, padx=(0, 8))

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
        _attach_hover_tooltip(
            _frb,
            "Clears all saved devices, pyatv credentials, discovery cache, and purges pigeonPulledMedia / pigeonReformattedMedia.",
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
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS):
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
        # While clock saver is active, a tap brightens the saver briefly without resetting idle (see CLOCK_SAVER_PEEK_S).
        def _on_label_button_release_peek_or_bump(event: tk.Event) -> None:
            nonlocal skip_cache
            if _PIGEON_EXT and clock_saver_composite_bgra is not None:
                now_e = time.monotonic()
                if _clock_saver_active(now_e):
                    clock_saver_peek_until_mono[0] = now_e + CLOCK_SAVER_PEEK_S
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
            hud_bar,
            hud,
            command_bar,
            settings_frame,
            settings_scroll_outer,
            settings_canvas,
            settings_inner,
            settings_footer_row,
        ):
            _prepend_hotkey_bindtag(w)

        for seq in ("<KeyPress-Tab>", "<Key-Tab>"):
            root.bind_class(HOTKEY_BINDTAG, seq, on_tab_key)
        root.bind_class(HOTKEY_BINDTAG, "<Control-KeyPress-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<Control-Key-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-s>", on_s_key)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-S>", on_s_key)

        def on_return_overlay_command(event: tk.Event) -> str | None:
            _bump_pigeon_user_activity(event)
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS):
                return None
            w = event.widget
            if w == command_entry or str(w) == str(command_entry):
                return None
            if _widget_accepts_typing(w):
                return None
            if command_entry_visible:
                try:
                    command_entry.focus_force()
                except tk.TclError:
                    command_entry.focus_set()
            else:
                show_command_entry()
            return "break"

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

                def _work_atv() -> None:
                    try:
                        from pigeon.apple_tv_now_playing import send_play_pause_to_device

                        send_play_pause_to_device(
                            device_identifier=ident,
                            device_address=addr,
                            scan_timeout_s=8,
                        )
                    except Exception:
                        pass

                threading.Thread(target=_work_atv, daemon=True).start()
                return True
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

        def on_shift_tab_advanced(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            if not _PIGEON_EXT:
                return None
            _bump_pigeon_user_activity(event)
            now = time.monotonic()
            if now - _last_adv_shift_tab_mono[0] < 0.35:
                return "break"
            _last_adv_shift_tab_mono[0] = now
            _open_advanced_capability_matrix()
            return "break"

        for _adv_hot in (
            "<KeyPress-ISO_Left_Tab>",
            "<Shift-KeyPress-Tab>",
            "<Shift-Key-Tab>",
        ):
            root.bind_all(_adv_hot, on_shift_tab_advanced)
        for _tmdb_key in ("<KeyPress-question>", "<Shift-KeyPress-slash>"):
            root.bind_all(_tmdb_key, on_tmdb_retry_hotkey)
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
                    backdrop_master_bgr, w, h, app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit
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
            _start_location_toast()

        def render_once() -> None:
            nonlocal last_frame, brightness_current, scaled_display, scaled_version, skip_cache, black_photo
            nonlocal scene_enabled, playing, use_backdrop_scene, backdrop_master_bgr

            now = time.monotonic()
            if (
                _PIGEON_EXT
                and not _startup_splash_complete[0]
                and (now - _pigeon_ui_started_mono) >= STARTUP_PIGEON_WORDMARK_MAX_S
                and dev_phase != DevPhase.SETTINGS
            ):
                _startup_splash_complete[0] = True
                if (
                    saved_backdrop_master_bgr is not None
                    and scene_enabled
                    and not use_backdrop_scene
                ):
                    apply_saved_tmdb_backdrop_to_display()
                    return
                skip_cache = None
                _warm_playback_overlay_blits()

            if dev_phase == DevPhase.SETTINGS:
                root.after(paused_interval_ms, render_once)
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
                    tk_img = _bgr_to_tk_image(out_bgr)
                    label.configure(image=tk_img)
                    label.image = tk_img
                else:
                    if black_photo is None:
                        black_photo = _bgr_to_tk_image(_black_screen_bgr())
                    label.configure(image=black_photo)
                    label.image = black_photo
                root.after(paused_interval_ms, render_once)
                return

            if use_backdrop_scene:
                if backdrop_master_bgr is None:
                    use_backdrop_scene = False
            if not use_backdrop_scene:
                if last_frame is None:
                    root.after(frame_interval_ms if playing else paused_interval_ms, render_once)
                    return
                if not _PIGEON_EXT and scaled_display is None:
                    root.after(frame_interval_ms if playing else paused_interval_ms, render_once)
                    return

            brightness_animating = abs(brightness_current - brightness_target) > 1e-4
            # TMDb backdrop: fixed level; paused video uses 0.3.
            b_scene = BACKDROP_BRIGHTNESS if use_backdrop_scene else brightness_current
            b_key = round(float(b_scene), 4)
            # Clock text changes every second; include wall time when widgets are active.
            tick_key = int(time.time()) if _PIGEON_EXT else 0
            idle_s_here = (
                max(0.0, min(1.0, _compose_idle_strength_holder[0])) if _PIGEON_EXT else 0.0
            )
            idle_want_here = 1.0 if _atv_idle_monochrome_active() else 0.0
            # While easing toward dim or back to full bright, always composite (skip-cache can quantize away steps).
            idle_dim_animating = _PIGEON_EXT and abs(idle_s_here - idle_want_here) > 1e-4
            idle_cache_key = int(round(idle_s_here * 500)) if _PIGEON_EXT else 0
            ta_toast = _location_toast_alpha(now) if _PIGEON_EXT else 0.0
            location_toast_animating = _PIGEON_EXT and 0.0 < ta_toast < 1.0
            location_toast_cache_key = int(round(ta_toast * 1000)) if _PIGEON_EXT else 0
            clock_saver_cache_key = 1 if (_PIGEON_EXT and _clock_saver_active(now)) else 0
            clock_saver_peek_cache_key = (
                1 if (_PIGEON_EXT and now < clock_saver_peek_until_mono[0]) else 0
            )
            startup_wm_cache_key = (
                1
                if (
                    _PIGEON_EXT
                    and (now - _pigeon_ui_started_mono) < STARTUP_PIGEON_WORDMARK_MAX_S
                )
                else 0
            )
            paused_row_cache_key = 1 if (_PIGEON_EXT and _show_paused_row_overlay()) else 0
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
            volume_boost_cache_key = _volume_boost_cache_key() if _PIGEON_EXT else 0

            if (
                not playing
                and not brightness_animating
                and not idle_dim_animating
                and not location_toast_animating
                and skip_cache
                == (
                    scaled_version,
                    b_key,
                    int(dev_phase),
                    tick_key,
                    display_dims[0],
                    display_dims[1],
                    1 if use_backdrop_scene else 0,
                    idle_cache_key,
                    location_toast_cache_key,
                    clock_saver_cache_key,
                    clock_saver_peek_cache_key,
                    startup_wm_cache_key,
                    paused_row_cache_key,
                    volume_boost_cache_key,
                    theater_dim_key,
                )
            ):
                root.after(paused_interval_ms, render_once)
                return

            if _PIGEON_EXT:
                shown = _compose_shown_frame(
                    last_frame if not use_backdrop_scene else None, b_scene
                )
                if lerp_bgr_red_monochrome is not None:
                    sm = max(0.0, min(1.0, _compose_idle_strength_holder[0]))
                    if sm > 1e-6:
                        shown = lerp_bgr_red_monochrome(shown, sm)
            else:
                shown = _apply_brightness(scaled_display, b_scene)
            tk_img = _bgr_to_tk_image(shown)
            label.configure(image=tk_img)
            label.image = tk_img

            if (
                not playing
                and not brightness_animating
                and not idle_dim_animating
                and not location_toast_animating
            ):
                skip_cache = (
                    scaled_version,
                    b_key,
                    int(dev_phase),
                    tick_key,
                    display_dims[0],
                    display_dims[1],
                    1 if use_backdrop_scene else 0,
                    idle_cache_key,
                    location_toast_cache_key,
                    clock_saver_cache_key,
                    clock_saver_peek_cache_key,
                    startup_wm_cache_key,
                    paused_row_cache_key,
                    volume_boost_cache_key,
                    theater_dim_key,
                )
            else:
                skip_cache = None

            root.after(frame_interval_ms if playing else paused_interval_ms, render_once)

        def _tick_volume_text_boost_anim() -> None:
            nonlocal skip_cache
            root.after(VOLUME_TEXT_BOOST_TICK_MS, _tick_volume_text_boost_anim)
            if not _PIGEON_EXT or playback_overlay_widget is None:
                return
            now = time.monotonic()
            dt = now - _volume_anim_last_mono[0]
            _volume_anim_last_mono[0] = now
            dt = max(0.0, min(0.08, dt))
            hf = _volume_hold_until_mono[0]
            target = 1.0 if now < hf else 0.0
            up_r = 1.0 / VOLUME_TEXT_BOOST_ANIM_UP_S
            dn_r = 1.0 / VOLUME_TEXT_BOOST_ANIM_DOWN_S
            s0 = _volume_boost_strength[0]
            s = s0
            if s < target - 1e-5:
                s = min(target, s + up_r * dt)
            elif s > target + 1e-5:
                s = max(target, s - dn_r * dt)
            else:
                s = target
            _volume_boost_strength[0] = s
            if _maybe_rewarm_volume_overlay_for_smooth_boost():
                skip_cache = None
                try:
                    render_once()
                except Exception:
                    pass

        def _receiver_poll_tick() -> None:
            root.after(RECEIVER_POLL_MS, _receiver_poll_tick)
            if not _PIGEON_EXT or playback_overlay_widget is None:
                return
            if receiver_poll_busy["active"]:
                return

            host = str(receiver_http_host.get("host") or "").strip()

            def apply_overlay(incoming: str, config: str, volume: str) -> None:
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
                _on_receiver_volume_display_changed(old_vol_raw, new_vol)
                receiver_overlay_state["incoming"] = incoming
                receiver_overlay_state["config"] = config
                receiver_overlay_state["volume"] = volume
                _apply_volume_boost_to_widget()
                if overlay_unchanged:
                    return
                _volume_boost_cache_k[0] = _volume_boost_cache_key()
                _warm_playback_overlay_blits()
                nonlocal skip_cache
                skip_cache = None
                render_once()

            receiver_poll_busy["active"] = True

            def work() -> None:
                from pigeon.widgets.playback_overlay import _receiver_volume_display_line

                r = None
                if host:
                    try:
                        from pigeon.receiver_denon import poll_denon_like_receiver

                        r = poll_denon_like_receiver(host, timeout=5.0)
                    except Exception:
                        r = None

                roku_line = ""
                try:
                    from pigeon.roku_ecp import (
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
                        roku_line = fetch_roku_playback_line(rbase_line, timeout=3.0) or ""
                except Exception:
                    roku_line = ""

                denon_vol_raw = ""
                if r is not None and r.ok:
                    denon_vol_raw = str(r.volume or "").strip()
                denon_vol_effective = (
                    denon_vol_raw if _receiver_volume_display_line(denon_vol_raw) else ""
                )
                merged_volume = denon_vol_effective or str(roku_line or "").strip()

                def apply() -> None:
                    rpl = receiver_panel_led_holder[0]
                    try:
                        denon_ok = r is not None and r.ok
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
                        if r is not None and r.ok:
                            apply_overlay(r.incoming, r.config, merged_volume)
                            if rpl is not None:
                                _paint_boolean_led(rpl, True)
                        elif merged_volume:
                            apply_overlay("", "", merged_volume)
                            if rpl is not None:
                                _paint_boolean_led(rpl, False)
                        else:
                            apply_overlay("", "", "")
                            if rpl is not None:
                                _paint_boolean_led(rpl, False)
                        row_b = streaming_slot_holder[0]
                        if row_b and not row_is_playback_apple_tv(row_b):
                            try:
                                from pigeon.roku_ecp import (
                                    fetch_roku_active_app_name,
                                    resolve_roku_ecp_base_url_for_row,
                                )

                                rbadge = str(
                                    resolve_roku_ecp_base_url_for_row(row_b) or ""
                                ).strip()
                                if rbadge:
                                    apnm = fetch_roku_active_app_name(rbadge)
                                    if apnm:
                                        _sync_streaming_badge_from_playback_sources(
                                            None,
                                            roku_app_name=apnm,
                                        )
                            except Exception:
                                pass
                    except tk.TclError:
                        pass

                root.after(0, apply)

            threading.Thread(target=work, daemon=True).start()

        shell.bind("<Configure>", _on_shell_configure)

        render_once()
        root.after(VOLUME_TEXT_BOOST_TICK_MS, _tick_volume_text_boost_anim)
        root.after(600, _receiver_poll_tick)

    root.after(1, bootstrap)
    try:
        root.mainloop()
    finally:
        if cap is not None:
            cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
