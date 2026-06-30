"""Idle clock saver: large centered time in Digital-7 (rows 2–top of 7) and date in the clock widget cell."""

from __future__ import annotations

import colorsys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.design import GRID_COLS, get_grid_geometry
from pigeon.font_paths import (
    resolve_digital7_font,
    resolve_ui_font_bold,
    resolve_ui_font_medium,
    resolve_ui_font_regular,
)
from pigeon.widgets.clock_calendar import (
    _resolve_display_time,
    _time_12h_no_leading_zero,
    clock_widget_design_rect,
)

# Large saver time: top of row 2 through top of row 7 (five grid rows).
_CLOCK_SAVER_TIME_ROW_TOP_1BASED = 2
_CLOCK_SAVER_TIME_ROW_END_1BASED = 7

_TEXT_WHITE = (255, 255, 255, 255)
_DATE_STROKE_W = 2
_DATE_STROKE_RGBA = (0, 0, 0, 255)
_SHADOW_ALPHA = 110
_DEFAULT_SHADOW_BGR = (40, 140, 255)
_TIME_ALPHA_BOOST = 1.1
_TIME_FONT_SCALE = 4.2

# Time color: 10s hold, 10s linear blend to the next stop (white → hue → hue → …).
_TIME_COLOR_SEGMENT_S = 10.0


def _time_color_rgba(now_mono: float) -> tuple[int, int, int, int]:
    """Hold current color 10s, then 10s transition to the next; timeline starts on white."""
    H = float(_TIME_COLOR_SEGMENT_S)
    t = max(0.0, float(now_mono))
    seg = int(t // H)
    u = (t % H) / H if H > 0 else 0.0

    def _rgb_block(k: int) -> tuple[float, float, float]:
        if k <= 0:
            return (1.0, 1.0, 1.0)
        hue = ((k - 1) * 0.3021688479) % 1.0
        return colorsys.hsv_to_rgb(hue, 0.72, 0.96)

    if seg % 2 == 0:
        r, g, b = _rgb_block(seg // 2)
        return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), 255)
    a = _rgb_block(seg // 2)
    b = _rgb_block(seg // 2 + 1)
    r = a[0] + (b[0] - a[0]) * u
    g = a[1] + (b[1] - a[1]) * u
    bl = a[2] + (b[2] - a[2]) * u
    return (int(round(r * 255)), int(round(g * 255)), int(round(bl * 255)), 255)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return r - l, b - t


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _font_path_usable(path: str | None) -> bool:
    if not path:
        return False
    try:
        ImageFont.truetype(path, 24)
        return True
    except OSError:
        return False


def _fit_font_in_box(
    text: str,
    font_path: str,
    max_w: int,
    max_h: int,
    draw: ImageDraw.ImageDraw,
    min_sz: int = 10,
) -> ImageFont.ImageFont:
    lo, hi = min_sz, max(max_h * 4, max_w, 400)
    best: ImageFont.ImageFont | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _load_font(font_path, mid)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        tw, th = r - l, b - t
        if tw <= max_w and th <= max_h:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1
    return best if best is not None else _load_font(font_path, min_sz)


def _fit_font_centered_mm_in_margins(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: str,
    *,
    w: int,
    h: int,
    cx: int,
    cy: int,
    pad_l: int,
    pad_r: int,
    pad_t: int,
    pad_b: int,
    shadow_off: int,
    min_sz: int = 10,
) -> ImageFont.ImageFont:
    lo, hi = min_sz, max(h * 4, w, 600)

    def fits(sz: int) -> bool:
        fnt = _load_font(font_path, sz)
        for ax, ay in ((cx, cy), (cx + shadow_off, cy + shadow_off)):
            bb = draw.textbbox((ax, ay), text, font=fnt, anchor="mm")
            if (
                bb[0] < pad_l - 1
                or bb[2] > w - pad_r + 1
                or bb[1] < pad_t - 1
                or bb[3] > h - pad_b + 1
            ):
                return False
        return True

    best: ImageFont.ImageFont | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(mid):
            best = _load_font(font_path, mid)
            lo = mid + 1
        else:
            hi = mid - 1
    return best if best is not None else _load_font(font_path, min_sz)


def clock_saver_time_design_rect() -> tuple[int, int, int, int]:
    """Pixel rect for large saver time: full grid width, rows 2 .. top of row 7."""
    g = get_grid_geometry()
    cell = g.cell
    x = g.x0
    w = GRID_COLS * cell
    r_top = int(_CLOCK_SAVER_TIME_ROW_TOP_1BASED)
    r_end = int(_CLOCK_SAVER_TIME_ROW_END_1BASED)
    r_top = max(1, r_top)
    r_end = max(r_top + 1, r_end)
    y = g.y0 + (r_top - 1) * cell
    h_time = max(1, (r_end - r_top) * cell)
    return (x, y, w, h_time)


def _apply_layer_opacity(bgra: np.ndarray, op: float) -> np.ndarray:
    o = max(0.0, min(1.0, float(op)))
    if o >= 0.999:
        return bgra
    out = bgra.astype(np.float32)
    out[:, :, 3] *= o
    return np.clip(out, 0, 255).astype(np.uint8)


def _date_bgra_clock_widget_rect(
    *,
    shadow_bgr: tuple[int, int, int] | None,
    layer_opacity: float,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Full month + numerical day + year in the same 5×1 cell as the live clock."""
    wx, wy, w, h = clock_widget_design_rect()
    now = _resolve_display_time()
    date_text = f"{now.strftime('%B')} {now.day}, {now.year}"

    medium_path = resolve_ui_font_medium() or resolve_ui_font_regular()

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, int(min(w, h) * 0.05))
    inner_w = max(1, w - 2 * pad)
    inner_h = max(1, h - 2 * pad)
    f_date = _fit_font_in_box(date_text, medium_path, inner_w, inner_h, draw, min_sz=6)

    x_right = w - pad
    baseline_y = h // 2
    bb = draw.textbbox((x_right, baseline_y), date_text, font=f_date, anchor="rm")
    cy = (bb[1] + bb[3]) // 2
    baseline_y += h // 2 - cy
    bb = draw.textbbox((x_right, baseline_y), date_text, font=f_date, anchor="rm")
    if bb[3] > h - pad:
        baseline_y -= bb[3] - (h - pad)
    if bb[1] < pad:
        baseline_y += pad - bb[1]

    b_bgr, g_bgr, r_bgr = (
        shadow_bgr
        if shadow_bgr and len(shadow_bgr) == 3
        else _DEFAULT_SHADOW_BGR
    )
    shadow_rgb = (int(r_bgr), int(g_bgr), int(b_bgr))
    shadow_fill = shadow_rgb + (_SHADOW_ALPHA,)
    off = max(1, int(round(min(w, h) * 0.018)))

    draw.text(
        (x_right + off, baseline_y + off),
        date_text,
        font=f_date,
        fill=shadow_fill,
        anchor="rm",
    )
    draw.text(
        (x_right, baseline_y),
        date_text,
        font=f_date,
        fill=_TEXT_WHITE,
        anchor="rm",
        stroke_width=_DATE_STROKE_W,
        stroke_fill=_DATE_STROKE_RGBA,
    )

    rgba = np.asarray(img)
    bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    bgra = _apply_layer_opacity(bgra, layer_opacity)
    return bgra, (wx, wy, w, h)


def _time_bgra_full_width_band(
    *,
    shadow_bgr: tuple[int, int, int] | None,
    layer_opacity: float,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    x0, y0, w, h_time = clock_saver_time_design_rect()
    now = _resolve_display_time()
    time_text = f"{_time_12h_no_leading_zero(now)} {now.strftime('%p').lower()}"

    time_font_path = resolve_digital7_font()
    if not _font_path_usable(time_font_path):
        time_font_path = resolve_ui_font_bold()

    img = Image.new("RGBA", (w, h_time), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad_lr = 1
    pad_t = 1
    pad_b = 1
    cx_t = w // 2
    cy_t = h_time // 2
    off = max(1, int(round(min(w, h_time) * 0.01)))

    f_time = _fit_font_centered_mm_in_margins(
        draw,
        time_text,
        time_font_path,
        w=w,
        h=h_time,
        cx=cx_t,
        cy=cy_t,
        pad_l=pad_lr,
        pad_r=pad_lr,
        pad_t=pad_t,
        pad_b=pad_b,
        shadow_off=off,
    )

    def _time_font_fits(fnt: ImageFont.ImageFont) -> bool:
        for ax, ay in ((cx_t, cy_t), (cx_t + off, cy_t + off)):
            bb = draw.textbbox((ax, ay), time_text, font=fnt, anchor="mm")
            if (
                bb[0] < pad_lr - 1
                or bb[2] > w - pad_lr + 1
                or bb[1] < pad_t - 1
                or bb[3] > h_time - pad_b + 1
            ):
                return False
        return True

    s0 = int(getattr(f_time, "size", 12))
    s_hi = max(s0 + 1, int(round(s0 * _TIME_FONT_SCALE)))
    for sz in range(s_hi, s0 - 1, -1):
        cand = _load_font(time_font_path, sz)
        if _time_font_fits(cand):
            f_time = cand
            break

    b_bgr, g_bgr, r_bgr = (
        shadow_bgr
        if shadow_bgr and len(shadow_bgr) == 3
        else _DEFAULT_SHADOW_BGR
    )
    shadow_rgb = (int(r_bgr), int(g_bgr), int(b_bgr))
    shadow_fill = shadow_rgb + (_SHADOW_ALPHA,)

    draw.text(
        (cx_t + off, cy_t + off),
        time_text,
        font=f_time,
        fill=shadow_fill,
        anchor="mm",
    )
    draw.text(
        (cx_t, cy_t),
        time_text,
        font=f_time,
        fill=_time_color_rgba(time.monotonic()),
        anchor="mm",
    )

    rgba = np.asarray(img)
    bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    bgra[:, :, 3] = np.clip(bgra[:, :, 3].astype(np.float32) * _TIME_ALPHA_BOOST, 0, 255).astype(
        np.uint8
    )
    bgra = _apply_layer_opacity(bgra, layer_opacity)
    return bgra, (x0, y0, w, h_time)


def clock_saver_composite_bgra(
    *,
    shadow_bgr: tuple[int, int, int] | None,
    layer_opacity: float = 1.0,
    time_layer_opacity: float | None = None,
    date_layer_opacity: float | None = None,
    date_anchor_row: int | None = None,
    date_anchor_col: int | None = None,
) -> tuple[
    tuple[np.ndarray, tuple[int, int, int, int]],
    tuple[np.ndarray, tuple[int, int, int, int]],
]:
    """
    Two patches: (1) large time band, (2) date in the clock-widget rectangle.

    ``layer_opacity`` is the default for both bands when the specific overrides are omitted.
    Pass ``time_layer_opacity=1.0`` and ``date_layer_opacity=layer_opacity`` to keep the
    large time at full brilliance while dimming the date during idle saver.

    ``date_anchor_*`` are ignored (date always uses ``clock_widget_design_rect()``).
    """
    _ = (date_anchor_row, date_anchor_col)
    t_op = float(layer_opacity if time_layer_opacity is None else time_layer_opacity)
    d_op = float(layer_opacity if date_layer_opacity is None else date_layer_opacity)
    time_pack = _time_bgra_full_width_band(
        shadow_bgr=shadow_bgr,
        layer_opacity=t_op,
    )
    date_pack = _date_bgra_clock_widget_rect(
        shadow_bgr=shadow_bgr,
        layer_opacity=d_op,
    )
    return time_pack, date_pack
