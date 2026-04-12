"""One-line location label on the main UI (design grid)."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.design import rect_for_span_at_cell
from pigeon.font_paths import resolve_ui_font_medium
from pigeon.widgets.clock_calendar import (
    design_clock_calendar_medium_font_point_size,
    design_clock_text_left_inset_in_span,
)

LOCATION_TOAST_FULL_S = 15.0
# Medium-face point size relative to the fitted clock line (calendar uses the same medium face).
LOCATION_TOAST_FONT_SCALE_VS_CLOCK = 0.375
LOCATION_TOAST_FADE_S = 2.0
# Top-left of the label patch in 1-based grid coordinates (same convention as rect_for_span_at_cell).
# Keep ``LOCATION_TOAST_GRID_COL`` equal to ``pigeon_0_5.CLOCK_ANCHOR_COL`` so the text lines up with the clock.
LOCATION_TOAST_GRID_ROW = 2
LOCATION_TOAST_GRID_COL = 11

_SPAN = (5, 1)
_TEXT_WHITE = (255, 255, 255, 255)
_STROKE_W = 2
_STROKE_RGBA = (0, 0, 0, 255)
_SHADOW_ALPHA = 110
_DEFAULT_SHADOW_BGR = (40, 140, 255)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return r - l, b - t


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _ellipsize(text: str, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, max_w: int) -> str:
    if _text_size(draw, text, font)[0] <= max_w:
        return text
    ell = "…"
    if _text_size(draw, ell, font)[0] > max_w:
        return ""
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid].rstrip() + ell
        if _text_size(draw, cand, font)[0] <= max_w:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best or ell


def _font_and_line_scaled_vs_clock(
    text: str,
    medium_path: str,
    inner_w: int,
    inner_h: int,
    draw: ImageDraw.ImageDraw,
) -> tuple[ImageFont.ImageFont, str]:
    """Target ~37.5% of the clock widget’s fitted medium size; shrink if the location row is too short."""
    base_px = design_clock_calendar_medium_font_point_size()
    target_px = max(6, int(round(base_px * LOCATION_TOAST_FONT_SCALE_VS_CLOCK)))
    line_raw = (text or "").strip() or "Location"
    min_sz = 6
    sz = target_px
    while sz >= min_sz:
        font = _load_font(medium_path, sz)
        line = _ellipsize(line_raw, draw, font, inner_w)
        l, t, r, b = draw.textbbox((0, 0), line, font=font, stroke_width=_STROKE_W)
        if (r - l) <= inner_w and (b - t) <= inner_h:
            return font, line
        sz -= 1
    font = _load_font(medium_path, min_sz)
    line = _ellipsize(line_raw, draw, font, inner_w)
    return font, line


def location_toast_patch_bgra(
    text: str,
    *,
    alpha: float,
    shadow_bgr: tuple[int, int, int] | None,
) -> tuple[np.ndarray | None, tuple[int, int, int, int]]:
    """
    BGRA patch spanning 5×1 cells; patch top-left is grid cell
    (LOCATION_TOAST_GRID_ROW, LOCATION_TOAST_GRID_COL). Text is left- and top-aligned in the patch.
    Returns (None, rect) if alpha is negligible.
    """
    a = max(0.0, min(1.0, float(alpha)))
    if a < 1e-6:
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=LOCATION_TOAST_GRID_ROW,
            col_1based=LOCATION_TOAST_GRID_COL,
        )
        return None, (wx, wy, w, h)

    wx, wy, w, h = rect_for_span_at_cell(
        _SPAN[0],
        _SPAN[1],
        row_1based=LOCATION_TOAST_GRID_ROW,
        col_1based=LOCATION_TOAST_GRID_COL,
    )
    pad = max(2, int(min(w, h) * 0.06))
    inner_w = max(1, w - 2 * pad)
    inner_h = max(1, h - 2 * pad)

    medium_path = resolve_ui_font_medium()
    if not medium_path:
        medium_path = "/System/Library/Fonts/Supplemental/Arial.ttf"

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font, line = _font_and_line_scaled_vs_clock(text, medium_path, inner_w, inner_h, draw)
    # Left-align with the clock time (default clock anchor row 1, col 13 — keep ``LOCATION_TOAST_GRID_COL`` in sync).
    try:
        text_left = design_clock_text_left_inset_in_span(
            anchor_row=1, anchor_col=LOCATION_TOAST_GRID_COL
        )
    except Exception:
        text_left = pad
    x0 = max(pad, int(text_left))
    y0 = pad

    acc = shadow_bgr if shadow_bgr and len(shadow_bgr) == 3 else _DEFAULT_SHADOW_BGR
    b_bgr, g_bgr, r_bgr = int(acc[0]), int(acc[1]), int(acc[2])
    shadow_rgb = (r_bgr, g_bgr, b_bgr)
    shadow_fill = shadow_rgb + (_SHADOW_ALPHA,)
    off = max(1, int(round(min(w, h) * 0.014)))

    draw.text((x0 + off, y0 + off), line, font=font, fill=shadow_fill, anchor="lt")
    draw.text(
        (x0, y0),
        line,
        font=font,
        fill=_TEXT_WHITE,
        anchor="lt",
        stroke_width=_STROKE_W,
        stroke_fill=_STROKE_RGBA,
    )

    rgba = np.asarray(img)
    bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    if a < 0.999:
        bgra = bgra.astype(np.float32)
        bgra[:, :, 3] *= a
        bgra = np.clip(bgra, 0, 255).astype(np.uint8)
    return bgra, (wx, wy, w, h)
