"""Full-width idle “screen saver” clock: date in the same 5×2 grid cell as ``ClockCalendarWidget``; large time in row 3+.

Each patch is an RGBA texture: areas outside the glyphs are **fully transparent** (alpha 0) — there is no solid
black plate behind the whole widget rectangle. Text is drawn in white with an opaque black stroke and a
semi-transparent tinted shadow; global idle opacity scales alphas (time and date can use separate opacities).
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.design import GRID_COLS, get_grid_geometry, rect_for_span_at_cell
from pigeon.font_paths import (
    resolve_digital7_font,
    resolve_ui_font_bold,
    resolve_ui_font_medium,
)
from pigeon.widgets.clock_calendar import _resolve_display_time, _time_12h_no_leading_zero

# Time band: starts at row 3; tall enough for digital-7 segments + shadow without bottom clipping.
_CLOCK_SAVER_TIME_HEIGHT_CELLS = 4.5
# Date uses the same grid footprint as ``ClockCalendarWidget`` (see pigeon_0_5.CLOCK_ANCHOR_*).
_CLOCK_WIDGET_SPAN = (5, 2)

_TEXT_WHITE = (255, 255, 255, 255)
_DATE_STROKE_W = 2
_DATE_STROKE_RGBA = (0, 0, 0, 255)
_SHADOW_ALPHA = 110
_DEFAULT_SHADOW_BGR = (40, 140, 255)
# Time text reads ~10% brighter than the date (higher alpha before global ``layer_opacity``).
_TIME_ALPHA_BOOST = 1.1


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return r - l, b - t


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


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
    """
    Binary-search font size using the same anchor as draw (``mm``), so bbox includes full glyph + shadow extent.
    ``(0,0)``-anchored metrics used in ``_fit_font_in_box`` are often too tight for LCD-style fonts.
    """
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
    """Pixel rect for the large time band (full grid width, rows 3 through 3 + ``_CLOCK_SAVER_TIME_HEIGHT_CELLS``)."""
    g = get_grid_geometry()
    cell = g.cell
    x = g.x0
    w = GRID_COLS * cell
    y = g.y0 + (3 - 1) * cell
    h_time = int(round(_CLOCK_SAVER_TIME_HEIGHT_CELLS * cell))
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
    anchor_row: int,
    anchor_col: int,
    shadow_bgr: tuple[int, int, int] | None,
    layer_opacity: float,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Month + day in the same 5×2 cell as the main clock widget (row-1 vertical centering)."""
    wx, wy, w, h = rect_for_span_at_cell(
        _CLOCK_WIDGET_SPAN[0],
        _CLOCK_WIDGET_SPAN[1],
        row_1based=anchor_row,
        col_1based=anchor_col,
    )
    now = _resolve_display_time()
    month_s = now.strftime("%B").lower()
    date_text = f"{month_s} {now.day}"

    medium_path = resolve_ui_font_medium()
    if not medium_path:
        medium_path = "/System/Library/Fonts/Supplemental/Arial.ttf"

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, int(min(w, h) * 0.04))
    inner_w = max(1, w - 2 * pad)
    row1_h = max(1, h // 2)
    inner_h = max(1, row1_h - max(1, pad // 2))
    f_date = _fit_font_in_box(date_text, medium_path, inner_w, inner_h, draw)

    tw, th = _text_size(draw, date_text, f_date)
    x0 = max(pad, w - pad - tw)
    baseline_y = row1_h // 2
    bb = draw.textbbox((x0, baseline_y), date_text, font=f_date, anchor="ls")
    cy = (bb[1] + bb[3]) // 2
    baseline_y += row1_h // 2 - cy

    b_bgr, g_bgr, r_bgr = (
        shadow_bgr
        if shadow_bgr and len(shadow_bgr) == 3
        else _DEFAULT_SHADOW_BGR
    )
    shadow_rgb = (int(r_bgr), int(g_bgr), int(b_bgr))
    shadow_fill = shadow_rgb + (_SHADOW_ALPHA,)
    off = max(1, int(round(min(w, h) * 0.018)))

    draw.text(
        (x0 + off, baseline_y + off),
        date_text,
        font=f_date,
        fill=shadow_fill,
        anchor="ls",
    )
    draw.text(
        (x0, baseline_y),
        date_text,
        font=f_date,
        fill=_TEXT_WHITE,
        anchor="ls",
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
    if not time_font_path:
        time_font_path = resolve_ui_font_bold()
    if not time_font_path:
        time_font_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

    img = Image.new("RGBA", (w, h_time), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad_lr = max(6, int(min(w, h_time) * 0.022))
    pad_t = max(6, int(min(w, h_time) * 0.028))
    pad_b = max(20, int(min(w, h_time) * 0.13))
    cx_t = w // 2
    # Slight upward bias: digital segments + shadow need more slack at the bottom of the band.
    cy_t = h_time // 2 - max(2, h_time // 48)
    off = max(1, int(round(min(w, h_time) * 0.012)))

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
    draw.text((cx_t, cy_t), time_text, font=f_time, fill=_TEXT_WHITE, anchor="mm")

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
    clock_anchor_row: int = 1,
    clock_anchor_col: int = 13,
) -> tuple[
    tuple[np.ndarray, tuple[int, int, int, int]],
    tuple[np.ndarray, tuple[int, int, int, int]],
]:
    """
    Two patches (design coordinates):

    1. **Time** — full grid width, starting row 3 (height ``_CLOCK_SAVER_TIME_HEIGHT_CELLS`` cells).
    2. **Date** — month + day in the same 5×2 rect as ``ClockCalendarWidget`` at ``(clock_anchor_row, clock_anchor_col)``.

    If ``time_layer_opacity`` / ``date_layer_opacity`` are omitted, both use ``layer_opacity``.
    """
    t_op = float(layer_opacity if time_layer_opacity is None else time_layer_opacity)
    d_op = float(layer_opacity if date_layer_opacity is None else date_layer_opacity)
    time_pack = _time_bgra_full_width_band(
        shadow_bgr=shadow_bgr,
        layer_opacity=t_op,
    )
    date_pack = _date_bgra_clock_widget_rect(
        anchor_row=clock_anchor_row,
        anchor_col=clock_anchor_col,
        shadow_bgr=shadow_bgr,
        layer_opacity=d_op,
    )
    return time_pack, date_pack
