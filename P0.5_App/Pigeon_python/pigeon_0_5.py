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
import tkinter.messagebox as messagebox

POSTER_ANCHOR_ROW = 1
POSTER_ANCHOR_COL = 3
# 5×2 clock anchored at [7,3]; poster 4×6 at [1,3].
CLOCK_ANCHOR_ROW = 7
CLOCK_ANCHOR_COL = 3

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from pigeon.app_state import auto_delete_pulled_media, read_app_state, write_app_state
from pigeon.hotkeys import format_hotkey_help_text
from pigeon.layout_paths import pick_default_scene_video, scene_dir_candidates
from pigeon.media_folders import (
    consolidate_legacy_pigeondata_media_folders,
    pigeon_pulled_media_dir,
    pigeon_reformatted_media_dir,
    purge_directory_contents,
)
from pigeon.stage_background import bgr_to_tk_hex, get_stage_bgr, set_stage_bgr

try:
    from pigeon.compositing import alpha_blend_bgra_over_bgr, scale_height_and_center_crop
    from pigeon.design import DESIGN_H, DESIGN_W, rect_for_span_at_cell
    from pigeon.overlay import blend_overlay_bgr, build_stage_overlay_source_bgra
    from pigeon.widgets.clock_calendar import ClockCalendarWidget
    from pigeon.widgets.poster_art import (
        PosterArtWidget,
        apply_poster_command_pigeon,
        apply_poster_command_terminator,
        prepare_default_poster_at_startup,
    )

    _PIGEON_EXT = True
except ImportError:
    alpha_blend_bgra_over_bgr = None  # type: ignore[misc, assignment]
    scale_height_and_center_crop = None  # type: ignore[misc, assignment]
    rect_for_span_at_cell = None  # type: ignore[misc, assignment]
    DESIGN_W = DESIGN_H = 0
    blend_overlay_bgr = None  # type: ignore[misc, assignment]
    build_stage_overlay_source_bgra = None  # type: ignore[misc, assignment]
    PosterArtWidget = None  # type: ignore[misc, assignment]
    apply_poster_command_pigeon = None  # type: ignore[misc, assignment]
    apply_poster_command_terminator = None  # type: ignore[misc, assignment]
    prepare_default_poster_at_startup = None  # type: ignore[misc, assignment]
    ClockCalendarWidget = None  # type: ignore[misc, assignment]
    _PIGEON_EXT = False


WINDOW_W = 800
WINDOW_H = 400
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
BACKDROP_BRIGHTNESS = 1.0

HOTKEY_BINDTAG = "Pigeon0_5_hotkeys"


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


def _guess_default_scene_path() -> str:
    env = os.environ.get("PIGEON_SCENE")
    if env:
        return env

    stem = "SCEENE_001_60SECS_scarytrain_718x300"
    exts = (".mp4", ".mov")
    candidates: list[str] = []
    for ext in exts:
        for scenes in scene_dir_candidates():
            candidates.append(str(scenes / f"{stem}{ext}"))

    for p in candidates:
        if os.path.isfile(p):
            return p

    picked = pick_default_scene_video()
    if picked is not None:
        sys.stderr.write(f"pigeon: default scene (discovered) → {picked}\n")
        sys.stderr.flush()
        return str(picked)

    return candidates[0] if candidates else stem + ".mp4"


def _open_capture(path: str) -> cv2.VideoCapture:
    expanded = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(expanded):
        raise FileNotFoundError(f"Scene file not found:\n{expanded}")
    cap = cv2.VideoCapture(expanded)
    if not cap.isOpened():
        cap.release()
        raise FileNotFoundError(f"Could not open video:\n{expanded}")
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    return cap


def _get_fps(cap: cv2.VideoCapture) -> float:
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if not np.isfinite(fps) or fps <= 1e-3:
        return 30.0
    return fps


def _ui_schedule_fps(cap: cv2.VideoCapture) -> float:
    """FPS for Tk after(); clamped so timing stays realistic if container metadata lies."""
    return float(min(max(_get_fps(cap), 12.0), 60.0))


def _read_frame(cap: cv2.VideoCapture) -> tuple[bool, np.ndarray | None]:
    ok, frame = cap.read()
    if not ok or frame is None:
        return False, None
    return True, frame


def _loop_to_start(cap: cv2.VideoCapture) -> None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)


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


def main() -> int:
    sys.stderr.write(f"pigeon: running script {os.path.abspath(__file__)}\n")
    sys.stderr.flush()

    parser = argparse.ArgumentParser(prog="Pigeon 0.5", add_help=True)
    parser.add_argument(
        "--scene",
        default=_guess_default_scene_path(),
        help="Path to scene MP4 (or set PIGEON_SCENE).",
    )
    args = parser.parse_args()

    scene_path = os.path.abspath(os.path.expanduser(args.scene))
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

    shell = tk.Frame(root, bg="#111")
    shell.pack(fill=tk.BOTH, expand=True)

    loading = tk.Label(
        shell,
        text="Opening scene…\n\n"
        "Top-right: Video = black (closes file) / Dev = developer mode.\n"
        "Tab cycles developer mode: off → grid overlay → settings → off. "
        "Return opens the command bar while in developer mode. "
        "Esc closes the bar or quits. F10 / double-click toggles video. Space = play.",
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

        def fail(msg: str) -> None:
            loading.configure(text=msg)
            sys.stderr.write(f"{msg}\n")
            sys.stderr.flush()

        try:
            cap = _open_capture(scene_path)
        except Exception as e:
            fail(f"Could not open scene:\n{e}")
            return

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

        def on_purge_pulled() -> None:
            ok, msg = purge_directory_contents(pigeon_pulled_media_dir())
            if ok:
                messagebox.showinfo("Purge pulled media", msg)
            else:
                messagebox.showerror("Purge pulled media", msg)

        def on_purge_reformatted() -> None:
            ok, msg = purge_directory_contents(pigeon_reformatted_media_dir())
            if ok:
                messagebox.showinfo("Purge reformatted media", msg)
            else:
                messagebox.showerror("Purge reformatted media", msg)

        auto_delete_var = tk.BooleanVar(value=auto_delete_pulled_media())
        settings_inner = tk.Frame(settings_frame, bg="#111")
        settings_inner.pack(expand=True, fill=tk.BOTH, padx=16, pady=12)
        tk.Label(
            settings_inner,
            text="Developer settings",
            fg="#eee",
            bg="#111",
            font=("Helvetica", 14, "bold"),
        ).pack(anchor=tk.W)
        tk.Checkbutton(
            settings_inner,
            text="Automatically delete pulled media (pigeonPulledMedia) after reformat",
            variable=auto_delete_var,
            bg="#111",
            fg="#ddd",
            selectcolor="#222",
            activebackground="#111",
            activeforeground="#ddd",
            highlightthickness=0,
            command=lambda: write_app_state(auto_delete_pulled_media=bool(auto_delete_var.get())),
        ).pack(anchor=tk.W, pady=(8, 4))
        purge_row = tk.Frame(settings_inner, bg="#111")
        purge_row.pack(anchor=tk.W, pady=6)
        tk.Button(
            purge_row,
            text="Purge pulled media",
            command=on_purge_pulled,
            font=("Helvetica", 10),
            padx=8,
            pady=4,
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            purge_row,
            text="Purge reformatted media",
            command=on_purge_reformatted,
            font=("Helvetica", 10),
            padx=8,
            pady=4,
        ).pack(side=tk.LEFT)
        tk.Label(
            settings_inner,
            text="Hotkeys",
            fg="#ccc",
            bg="#111",
            font=("Helvetica", 11, "bold"),
        ).pack(anchor=tk.W, pady=(16, 4))
        hotkey_text = tk.Text(
            settings_inner,
            height=16,
            width=80,
            wrap=tk.WORD,
            bg="#1a1a1e",
            fg="#e0e0e0",
            insertbackground="#e0e0e0",
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#333",
            font=("Courier", 10),
        )
        hotkey_text.pack(fill=tk.BOTH, expand=True)
        hotkey_text.insert("1.0", format_hotkey_help_text())
        hotkey_text.configure(state=tk.DISABLED)

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

        fps_sched = _ui_schedule_fps(cap)
        display_dims = [WINDOW_W, WINDOW_H]
        fit_holder = [SceneFit(target_w=WINDOW_W, target_h=WINDOW_H)]

        def _disp_fit() -> SceneFit:
            return fit_holder[0]

        def _black_screen_bgr() -> np.ndarray:
            sb, sg, sr = get_stage_bgr()
            out = np.empty((display_dims[1], display_dims[0], 3), dtype=np.uint8)
            out[:] = (sb, sg, sr)
            return out

        frame_interval_ms = max(1, int(round(1000.0 / fps_sched)))

        playing = False
        last_frame: np.ndarray | None = None
        brightness_current = 0.3
        brightness_from = 0.3
        brightness_target = 0.3
        brightness_t0 = time.monotonic()
        brightness_duration_s = 3.0
        brightness_duration_up_s = 1.0
        brightness_duration_down_s = 1.0

        ok, frame = _read_frame(cap)
        if ok and frame is not None:
            last_frame = frame
            _loop_to_start(cap)

        scaled_display: np.ndarray | None = None
        scaled_version = 0
        if last_frame is not None:
            scaled_display = _disp_fit().scale_and_crop(last_frame)
            scaled_version = 1

        skip_cache: tuple[int, float, int, int, int, int, int] | None = None
        scene_enabled = _load_persisted_scene_enabled(True)
        dev_phase = DevPhase.OFF
        saved_resume_frame = float(cap.get(cv2.CAP_PROP_POS_FRAMES))
        black_photo: ImageTk.PhotoImage | None = None
        use_backdrop_scene = False
        backdrop_master_bgr: np.ndarray | None = None
        # Last TMDb backdrop (copy); survives scene off / video on so developer-grid F10 can return to backdrop.
        saved_backdrop_master_bgr: np.ndarray | None = None

        if _PIGEON_EXT and prepare_default_poster_at_startup is not None:
            try:
                ok_sp, msg_sp, _gc_sp = prepare_default_poster_at_startup()
                sys.stderr.write(f"pigeon: startup poster: {msg_sp}\n")
                sys.stderr.flush()
            except Exception as e:
                sys.stderr.write(f"pigeon: startup poster error: {e}\n")
                sys.stderr.flush()

        poster_widget = (
            PosterArtWidget(anchor_row=POSTER_ANCHOR_ROW, anchor_col=POSTER_ANCHOR_COL)
            if _PIGEON_EXT and PosterArtWidget is not None
            else None
        )
        clock_widget = (
            ClockCalendarWidget(anchor_row=CLOCK_ANCHOR_ROW, anchor_col=CLOCK_ANCHOR_COL)
            if _PIGEON_EXT and ClockCalendarWidget is not None
            else None
        )

        poster_patch_bgra: np.ndarray | None = None
        clock_patch_bgra: np.ndarray | None = None
        _last_clock_design_tick = -1

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

        def _warm_poster_design_patch() -> None:
            nonlocal poster_patch_bgra
            if poster_widget is None:
                return
            poster_patch_bgra = poster_widget.bgra_patch().copy()

        def _refresh_clock_patch_bgra() -> None:
            nonlocal clock_patch_bgra, _last_clock_design_tick
            if clock_widget is None:
                return
            t = int(time.time())
            if t == _last_clock_design_tick and clock_patch_bgra is not None:
                return
            clock_patch_bgra = clock_widget.bgra_patch().copy()
            _last_clock_design_tick = t

        def compose_display_fast_no_grid(
            frame_bgr: np.ndarray | None,
            brightness: float,
            *,
            frame_is_display_sized: bool = False,
        ) -> np.ndarray:
            """Video at display size + poster/clock blits (no full design canvas). Used when developer grid is off."""
            assert _PIGEON_EXT
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
            _refresh_clock_patch_bgra()
            if (
                poster_patch_bgra is not None
                and poster_widget is not None
                and rect_for_span_at_cell is not None
                and alpha_blend_bgra_over_bgr is not None
            ):
                sw, sh = poster_widget.grid_span
                ar, ac = poster_widget.grid_anchor
                wx, wy, ww, wh = rect_for_span_at_cell(sw, sh, row_1based=ar, col_1based=ac)
                x, y, rw, rh = _design_rect_to_target(wx, wy, ww, wh, cap_w, cap_h)
                patch = cv2.resize(poster_patch_bgra, (rw, rh), interpolation=cv2.INTER_LINEAR)
                sub = base[y : y + rh, x : x + rw]
                sub[:] = alpha_blend_bgra_over_bgr(sub, patch)
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
            if poster_widget is not None:
                poster_widget.render(canvas)
            if clock_widget is not None:
                clock_widget.render(canvas)
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
                from pigeon.image_ui_protocol import backdrop_scene_bgr_for_display

                if not _PIGEON_EXT:
                    bd = backdrop_scene_bgr_for_display(
                        backdrop_master_bgr, display_dims[0], display_dims[1]
                    )
                    return _apply_brightness(bd, brightness)
                if dev_phase == DevPhase.GRID:
                    bd_d = backdrop_scene_bgr_for_display(backdrop_master_bgr, DESIGN_W, DESIGN_H)
                    return compose_display_from_source(
                        bd_d, brightness, show_grid=True, frame_is_design_sized=True
                    )
                dw, dh = display_dims[0], display_dims[1]
                cap_w = min(dw, MAX_FAST_COMPOSITE_W)
                cap_h = max(1, int(round(dh * (cap_w / float(dw))))) if dw > 0 else dh
                use_cap = cap_w < dw
                tw, th = (cap_w, cap_h) if use_cap else (dw, dh)
                bd_f = backdrop_scene_bgr_for_display(backdrop_master_bgr, tw, th)
                return compose_display_fast_no_grid(bd_f, brightness, frame_is_display_sized=True)

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
            _warm_poster_design_patch()

        # Start with video fully closed if last session had scene off — black screen, no capture.
        if not scene_enabled and cap is not None:
            cap.release()
            cap = None
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
            hud_h = max(28, int(OVERLAY_HUD_H * ui))
            bw, bh = max(40, int(56 * ui)), max(20, int(24 * ui))
            video_btn.place(x=int(dw - 118 * ui), y=int(4 * ui), width=bw, height=bh)
            dev_btn.place(x=int(dw - 58 * ui), y=int(4 * ui), width=max(36, int(54 * ui)), height=bh)
            hud.configure(wraplength=max(80, dw - 24), font=("Helvetica", max(8, int(11 * ui))))
            if command_entry_visible:
                place_command_bar()

        def place_command_bar() -> None:
            dw, dh = display_dims[0], display_dims[1]
            ui = _ui_scale()
            hud_h = max(28, int(OVERLAY_HUD_H * ui))
            bar_h = max(24, int(32 * ui))
            yb = dh - hud_h - bar_h - int(4 * ui) if dev_phase != DevPhase.OFF else dh - bar_h - int(4 * ui)
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
            else:
                try:
                    settings_frame.pack_forget()
                except tk.TclError:
                    pass
                label.pack(fill=tk.BOTH, expand=True)

        def sync_developer_chrome() -> None:
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
                        "F10: scene → black → backdrop → scene (backdrop after TMDb) | dbl-click / Video | Dev"
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
                hud.configure(
                    text=(
                        "Developer mode (settings) — Tab: next | Return: command bar | Esc: close bar / quit | Video / Dev"
                    ),
                )
                hud_bar.place(x=0, y=dh - hud_h, width=dw, height=hud_h)
                hud_bar.lift()
            else:
                root.title("")
                label.configure(highlightthickness=0)
                hud_bar.place_forget()
                hide_command_entry()
            if command_entry_visible:
                place_command_bar()
                command_bar.lift()
            video_btn.lift()
            dev_btn.lift()

        def toggle_play(_event=None) -> None:
            nonlocal playing, brightness_from, brightness_target, brightness_t0, brightness_duration_s
            if not scene_enabled or cap is None:
                return
            playing = not playing
            brightness_from = brightness_current
            brightness_target = 1.0 if playing else 0.3
            brightness_duration_s = (
                brightness_duration_up_s if brightness_target > brightness_from else brightness_duration_down_s
            )
            brightness_t0 = time.monotonic()

        def quit_app(_event=None) -> None:
            root.quit()

        def cycle_dev_phase(_event=None) -> str:
            nonlocal dev_phase, skip_cache
            dev_phase = DevPhase((int(dev_phase) + 1) % 3)
            skip_cache = None
            sync_developer_chrome()
            return "break"

        def _open_scene_video_capture() -> bool:
            """Open scene file, restore ``saved_resume_frame``, fill ``last_frame`` / ``scaled_display``. Clears backdrop display flags."""
            nonlocal cap, last_frame, scaled_display, scaled_version, frame_interval_ms, use_backdrop_scene, backdrop_master_bgr
            use_backdrop_scene = False
            backdrop_master_bgr = None
            try:
                cap = _open_capture(scene_path)
            except Exception as e:
                sys.stderr.write(f"pigeon: could not reopen video: {e}\n")
                sys.stderr.flush()
                return False
            frame_interval_ms = max(1, int(round(1000.0 / _ui_schedule_fps(cap))))
            cap.set(cv2.CAP_PROP_POS_FRAMES, saved_resume_frame)
            ok2, fr = _read_frame(cap)
            if not ok2 or fr is None:
                _loop_to_start(cap)
                ok2, fr = _read_frame(cap)
            if not ok2 or fr is None:
                sys.stderr.write("pigeon: could not read frame when turning scene on\n")
                sys.stderr.flush()
                cap.release()
                cap = None
                return False
            last_frame = fr
            scaled_display = _disp_fit().scale_and_crop(last_frame)
            scaled_version += 1
            return True

        def toggle_scene(_event=None, *, require_overlay: bool = True) -> None:
            nonlocal cap, scene_enabled, saved_resume_frame, last_frame, scaled_display, scaled_version, skip_cache, black_photo, playing, frame_interval_ms, use_backdrop_scene, backdrop_master_bgr
            if require_overlay and dev_phase != DevPhase.GRID:
                return

            if scene_enabled:
                if cap is not None:
                    try:
                        saved_resume_frame = float(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    except Exception:
                        pass
                    cap.release()
                    cap = None
                playing = False
                scene_enabled = False
                use_backdrop_scene = False
                backdrop_master_bgr = None
            else:
                if not _open_scene_video_capture():
                    return
                scene_enabled = True

            _save_persisted_scene_enabled(scene_enabled)
            skip_cache = None

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

        def f10_cycle_scene_grid() -> None:
            """
            Developer grid only: F10 cycles scene file on → off → backdrop (if TMDb backdrop exists) → scene on.
            """
            nonlocal cap, scene_enabled, saved_resume_frame, last_frame, scaled_display, scaled_version, skip_cache, playing, frame_interval_ms, use_backdrop_scene, backdrop_master_bgr, brightness_current, brightness_from, brightness_target, brightness_t0

            video_on = scene_enabled and (not use_backdrop_scene) and cap is not None

            if use_backdrop_scene and backdrop_master_bgr is not None:
                if not _open_scene_video_capture():
                    scene_enabled = False
                    _save_persisted_scene_enabled(False)
                    skip_cache = None
                    render_once()
                    return
                scene_enabled = True
                playing = False
                brightness_current = brightness_from = brightness_target = 0.3
                brightness_t0 = time.monotonic()
                _save_persisted_scene_enabled(True)
                skip_cache = None
                render_once()
                return

            if video_on:
                if cap is not None:
                    try:
                        saved_resume_frame = float(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    except Exception:
                        pass
                    cap.release()
                    cap = None
                playing = False
                scene_enabled = False
                use_backdrop_scene = False
                backdrop_master_bgr = None
                _save_persisted_scene_enabled(False)
                skip_cache = None
                render_once()
                return

            if saved_backdrop_master_bgr is not None:
                if cap is not None:
                    try:
                        saved_resume_frame = float(cap.get(cv2.CAP_PROP_POS_FRAMES))
                    except Exception:
                        pass
                    cap.release()
                    cap = None
                playing = False
                backdrop_master_bgr = saved_backdrop_master_bgr.copy()
                use_backdrop_scene = True
                scene_enabled = True
                last_frame = None
                if not _PIGEON_EXT:
                    from pigeon.image_ui_protocol import backdrop_scene_bgr_for_display

                    scaled_display = backdrop_scene_bgr_for_display(
                        backdrop_master_bgr, display_dims[0], display_dims[1]
                    )
                else:
                    scaled_display = None
                scaled_version += 1
                brightness_current = brightness_from = brightness_target = BACKDROP_BRIGHTNESS
                brightness_t0 = time.monotonic()
                _save_persisted_scene_enabled(True)
                skip_cache = None
                render_once()
                return

            if not _open_scene_video_capture():
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
            try:
                label.focus_set()
            except tk.TclError:
                try:
                    root.focus_set()
                except tk.TclError:
                    pass

        def on_double_click_scene(_event: tk.Event | None = None) -> None:
            toggle_scene(require_overlay=False)

        # Always-visible controls (mouse) — does not depend on keyboard focus or bindtags.
        btn_style = {"font": ("Helvetica", 9), "padx": 4, "pady": 0}
        video_btn = tk.Button(
            shell,
            text="Video",
            command=lambda: toggle_scene(require_overlay=False),
            **btn_style,
        )
        dev_btn = tk.Button(shell, text="Dev", command=lambda: cycle_dev_phase(), **btn_style)
        video_btn.lift()
        dev_btn.lift()

        def show_command_entry(_event=None) -> None:
            nonlocal command_entry_visible
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS):
                return
            command_entry_visible = True
            place_command_bar()
            command_bar.lift()
            video_btn.lift()
            dev_btn.lift()
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
            q = query.strip()
            if not q:
                return

            def finish_tmdb(ok_m: bool, msg_m: str, backdrop_master: np.ndarray | None = None) -> None:
                nonlocal skip_cache, cap, scene_enabled, saved_resume_frame, last_frame, scaled_display, scaled_version, playing, use_backdrop_scene, backdrop_master_bgr, saved_backdrop_master_bgr, brightness_current, brightness_from, brightness_target, brightness_t0
                sys.stderr.write(f"pigeon: tmdb → {msg_m}\n")
                sys.stderr.flush()
                if not ok_m:
                    messagebox.showerror("TMDb poster", msg_m)
                    return
                if poster_widget is not None:
                    poster_widget.clear_bgra_cache()
                _warm_poster_design_patch()
                _refresh_stage_from_poster()
                if backdrop_master is not None:
                    if cap is not None:
                        try:
                            saved_resume_frame = float(cap.get(cv2.CAP_PROP_POS_FRAMES))
                        except Exception:
                            pass
                        cap.release()
                        cap = None
                    backdrop_master_bgr = backdrop_master
                    saved_backdrop_master_bgr = np.asarray(backdrop_master, dtype=np.uint8).copy()
                    use_backdrop_scene = True
                    scene_enabled = True
                    playing = False
                    last_frame = None
                    if not _PIGEON_EXT:
                        from pigeon.image_ui_protocol import backdrop_scene_bgr_for_display

                        scaled_display = backdrop_scene_bgr_for_display(
                            backdrop_master_bgr, display_dims[0], display_dims[1]
                        )
                    else:
                        scaled_display = None
                    scaled_version += 1
                    _save_persisted_scene_enabled(True)
                    # Backdrop is static image — not paused-video 0.3; use dedicated backdrop level.
                    brightness_current = brightness_from = brightness_target = BACKDROP_BRIGHTNESS
                    brightness_t0 = time.monotonic()

            def worker() -> None:
                try:
                    from pigeon.tmdb_poster import apply_tmdb_movie_query

                    ok_w, msg_w, bd_w = apply_tmdb_movie_query(q, prefer=prefer)  # type: ignore[arg-type]
                except Exception as e:
                    ok_w, msg_w, bd_w = False, str(e), None
                root.after(0, lambda o=ok_w, m=msg_w, b=bd_w: finish_tmdb(o, m, b))

            threading.Thread(target=worker, daemon=True).start()

        def submit_command_entry(_event=None) -> str:
            nonlocal skip_cache
            if dev_phase not in (DevPhase.GRID, DevPhase.SETTINGS):
                return "break"
            now_sub = time.monotonic()
            if now_sub - _last_command_submit_mono[0] < 0.2:
                return "break"
            _last_command_submit_mono[0] = now_sub
            text = command_entry.get().strip()
            key = text.lower()
            if text and _PIGEON_EXT and poster_widget is not None:
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
                elif key == "terminator" and apply_poster_command_terminator is not None:
                    try:
                        ok_t, msg_t = apply_poster_command_terminator()
                        sys.stderr.write(f"pigeon: terminator → {msg_t}\n")
                        if not ok_t:
                            sys.stderr.write("pigeon: (terminator command failed)\n")
                    except Exception as e:
                        sys.stderr.write(f"pigeon: terminator error: {e}\n")
                    sys.stderr.flush()
                    poster_widget.clear_bgra_cache()
                    _warm_poster_design_patch()
                    _refresh_stage_from_poster()
                elif key == "pigeon" and apply_poster_command_pigeon is not None:
                    try:
                        ok_p, msg_p = apply_poster_command_pigeon()
                        sys.stderr.write(f"pigeon: pigeon → {msg_p}\n")
                        if not ok_p:
                            sys.stderr.write("pigeon: (pigeon command failed)\n")
                    except Exception as e:
                        sys.stderr.write(f"pigeon: pigeon error: {e}\n")
                    sys.stderr.flush()
                    poster_widget.clear_bgra_cache()
                    _warm_poster_design_patch()
                    _refresh_stage_from_poster()
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
            settings_inner,
            hotkey_text,
        ):
            _prepend_hotkey_bindtag(w)

        for seq in ("<KeyPress-Tab>", "<Key-Tab>"):
            root.bind_class(HOTKEY_BINDTAG, seq, on_tab_key)
        root.bind_class(HOTKEY_BINDTAG, "<Control-KeyPress-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<Control-Key-Tab>", on_ctrl_tab)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-s>", on_s_key)
        root.bind_class(HOTKEY_BINDTAG, "<KeyPress-S>", on_s_key)

        def on_return_overlay_command(event: tk.Event) -> str | None:
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

        def on_space_play(event: tk.Event) -> str | None:
            if _widget_accepts_typing(event.widget):
                return None
            toggle_play()
            return "break"

        root.bind_all("<space>", on_space_play)

        def on_escape(event: tk.Event) -> str | None:
            if command_entry_visible:
                hide_command_entry()
                return "break"
            quit_app()
            return "break"

        root.bind_all("<Escape>", on_escape)
        root.bind_all("<KeyPress-F9>", lambda e: try_cycle_dev_phase(None))
        root.bind_all("<KeyPress-F10>", on_f10_key)
        root.bind_all("<Control-Shift-KeyPress-s>", lambda e: toggle_scene(require_overlay=False))
        root.bind_all("<Control-Shift-KeyPress-S>", lambda e: toggle_scene(require_overlay=False))

        def _focus_when_mapped(_event=None) -> None:
            try:
                root.focus_force()
            except tk.TclError:
                root.focus_set()

        root.bind("<Map>", _focus_when_mapped)
        root.after_idle(_focus_when_mapped)

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

                scaled_display = backdrop_scene_bgr_for_display(backdrop_master_bgr, w, h)
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

        def render_once() -> None:
            nonlocal last_frame, brightness_current, scaled_display, scaled_version, skip_cache, black_photo
            nonlocal scene_enabled, playing, use_backdrop_scene, backdrop_master_bgr

            if dev_phase == DevPhase.SETTINGS:
                root.after(paused_interval_ms, render_once)
                return

            now = time.monotonic()
            t = (now - brightness_t0) / brightness_duration_s if brightness_duration_s > 0 else 1.0
            if t <= 0.0:
                brightness_current = brightness_from
            elif t >= 1.0:
                brightness_current = brightness_target
            else:
                brightness_current = brightness_from + (brightness_target - brightness_from) * t

            if scene_enabled and playing and cap is not None and not use_backdrop_scene:
                ok2, frame2 = _read_frame(cap)
                if not ok2 or frame2 is None:
                    _loop_to_start(cap)
                    ok2, frame2 = _read_frame(cap)
                if ok2 and frame2 is not None:
                    last_frame = frame2
                    scaled_version += 1
                    if not _PIGEON_EXT:
                        scaled_display = _disp_fit().scale_and_crop(last_frame)

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
                root.after(paused_interval_ms, render_once)
                return

            if use_backdrop_scene:
                if backdrop_master_bgr is None:
                    use_backdrop_scene = False
            if not use_backdrop_scene:
                if cap is None or last_frame is None:
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

            if not playing and not brightness_animating and skip_cache == (
                scaled_version,
                b_key,
                int(dev_phase),
                tick_key,
                display_dims[0],
                display_dims[1],
                1 if use_backdrop_scene else 0,
            ):
                root.after(paused_interval_ms, render_once)
                return

            if _PIGEON_EXT:
                shown = _compose_shown_frame(
                    last_frame if not use_backdrop_scene else None, b_scene
                )
            else:
                shown = _apply_brightness(scaled_display, b_scene)
            tk_img = _bgr_to_tk_image(shown)
            label.configure(image=tk_img)
            label.image = tk_img

            if not playing and not brightness_animating:
                skip_cache = (
                    scaled_version,
                    b_key,
                    int(dev_phase),
                    tick_key,
                    display_dims[0],
                    display_dims[1],
                    1 if use_backdrop_scene else 0,
                )
            else:
                skip_cache = None

            root.after(frame_interval_ms if playing else paused_interval_ms, render_once)

        shell.bind("<Configure>", _on_shell_configure)

        render_once()

    root.after(1, bootstrap)
    try:
        root.mainloop()
    finally:
        if cap is not None:
            cap.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
