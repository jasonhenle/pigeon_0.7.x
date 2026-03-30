"""Clock + calendar widget: 5×2 squares on the global 19×8 grid.

Default anchor is [1,1] (widget dev). Main Pigeon uses ``anchor_row`` / ``anchor_col`` for placement.

Layout matches a 5-column × 2-row sheet (e.g. After Effects red column guides 1–5):
  Row 1: month in cols 1–3.5; date in cols 4–5 (from col 4).
  Row 2: time in cols 1–3, right-aligned to the right edge of box [2,3]; am/pm in col 4; weekday in col 5.

Typography: Sharp Sans Bold (see PIGEON_FONT). Each label is scaled to use the full row height
within its horizontal band (uniform row scale), subject to width.

The drawn clock/calendar is ``_CLOCK_CONTENT_SCALE`` (default 0.8) of the grid box, top-left aligned
so the anchor cell origin is unchanged.
"""

from __future__ import annotations

import os
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.design import rect_for_span_at_cell
from pigeon.font_paths import resolve_ui_font_bold

_SPAN = (5, 2)

# Clock/calendar glyphs fill this fraction of the 5×2 grid box; remainder is transparent (origin = top-left).
_CLOCK_CONTENT_SCALE = 0.8

# Default sample when enabling fake time via hotkey (Nov 30 Mon 12:55 pm)
DEFAULT_FAKE_TIME_STRING = "2020-11-30 12:55:00"

# AE reference sizes (informational; runtime sizing is box-driven)
_W75 = (191, 191, 191, 255)
_W100 = (255, 255, 255, 255)


def _text_size(font: ImageFont.ImageFont, text: str) -> tuple[int, int]:
    if hasattr(font, "getbbox"):
        l, t, r, b = font.getbbox(text)
        return r - l, b - t
    w, h = font.getsize(text)
    return w, h


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _fit_font_to_box(
    path: str,
    text: str,
    max_w: int,
    max_h: int,
    min_sz: int = 6,
    *,
    min_sz_floor: int | None = None,
) -> ImageFont.ImageFont:
    """Largest font size so text fits in max_w × max_h (row-filling).

    ``min_sz_floor``: try to use at least this point size (e.g. weekday vs date balance)
    if it still fits; otherwise keep the smaller fit.
    """
    if max_w < 4 or max_h < 4:
        return _load_font(path, min_sz)
    lo, hi = min_sz, max(max_h * 3, max_w, 120)
    best = _load_font(path, min_sz)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _load_font(path, mid)
        tw, th = _text_size(f, text)
        if tw <= max_w and th <= max_h:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1

    if min_sz_floor is None:
        return best

    fs = getattr(best, "size", min_sz)
    if fs >= min_sz_floor:
        return best

    # Bump toward floor: largest size ≥ min_sz_floor that still fits (narrow cols + short labels).
    hi2 = max(max_h * 3, max_w, 120)
    for sz in range(min(hi2, min_sz_floor + 80), min_sz_floor - 1, -1):
        f = _load_font(path, sz)
        tw, th = _text_size(f, text)
        if tw <= max_w and th <= max_h:
            return f
    return best


def _time_12h_no_leading_zero(now: datetime) -> str:
    h12 = now.hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{now.strftime('%M')}"


def _resolve_display_time() -> datetime:
    """
    Live clock uses ``datetime.now()``. For layout tests, set either:

    - ``PIGEON_FAKE_TIME`` or ``PIGEON_FAKE_DATETIME`` (e.g. ``2020-11-30 12:55:00``)
    - ``pigeon_widget_preview.py --fake-time "2020-11-30 12:55:00"``
    """
    raw = (os.environ.get("PIGEON_FAKE_TIME") or os.environ.get("PIGEON_FAKE_DATETIME") or "").strip()
    if not raw:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", ""))
    except ValueError:
        pass
    return datetime.now()


class ClockCalendarWidget:
    def __init__(self, *, anchor_row: int = 1, anchor_col: int = 1) -> None:
        self._anchor_row = anchor_row
        self._anchor_col = anchor_col

    @property
    def grid_span(self) -> tuple[int, int]:
        return _SPAN

    @property
    def grid_anchor(self) -> tuple[int, int]:
        """Top-left cell (1-based) on the global 19×8 grid."""
        return (self._anchor_row, self._anchor_col)

    def _rgba_clock_content(self, w: int, h: int) -> Image.Image:
        """Draw clock/calendar filling a transparent RGBA image of exactly ``w``×``h``."""
        cell = max(1, w // 5)
        row1_top = 0
        row1_h = max(1, h // 2)
        row2_top = row1_h
        row2_h = max(1, h - row1_h)

        now = _resolve_display_time()
        month_s = now.strftime("%B").lower()
        date_s = str(now.day)
        time_s = _time_12h_no_leading_zero(now)
        ampm_s = now.strftime("%p").lower()
        dow_s = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[now.weekday()]

        font_path = resolve_ui_font_bold()
        if not font_path:
            font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"

        month_box = (0, row1_top, int(3.5 * cell), row1_h)
        date_box = (int(3.5 * cell), row1_top, w - int(3.5 * cell), row1_h)
        time_box = (0, row2_top, 3 * cell, row2_h)
        ampm_box = (3 * cell, row2_top, cell, row2_h)
        day_box = (4 * cell, row2_top, cell, row2_h)

        pad = max(2, int(cell * 0.03))

        def inner(box: tuple[int, int, int, int]) -> tuple[int, int]:
            bx, by, bw, bh = box
            return max(1, bw - 2 * pad), max(1, bh - 2 * pad)

        mw, mh = inner(month_box)
        dw, dh = inner(date_box)
        tw, th = inner(time_box)
        aw, ah = inner(ampm_box)
        yw, yh = inner(day_box)

        month_font = _fit_font_to_box(font_path, month_s, mw, mh)
        date_font = _fit_font_to_box(font_path, date_s, dw, dh)
        time_font = _fit_font_to_box(font_path, time_s, tw, th)
        ampm_font = _fit_font_to_box(font_path, ampm_s, aw, ah)
        date_pt = getattr(date_font, "size", None)
        dow_floor = max(8, int((date_pt or 20) * 0.72))
        day_font = _fit_font_to_box(font_path, dow_s, yw, yh, min_sz_floor=dow_floor)

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        def center_in_box(
            text: str,
            font: ImageFont.ImageFont,
            box: tuple[int, int, int, int],
            fill: tuple[int, int, int, int],
        ) -> None:
            bx, by, bw, bh = box
            l, t, r, b = draw.textbbox((0, 0), text, font=font)
            tw_, th_ = r - l, b - t
            cx = bx + bw // 2
            cy = by + bh // 2
            x = cx - (tw_ // 2) - l
            y = cy - (th_ // 2) - t
            draw.text((x, y), text, font=font, fill=fill)

        def right_align_in_box(
            text: str,
            font: ImageFont.ImageFont,
            box: tuple[int, int, int, int],
            fill: tuple[int, int, int, int],
        ) -> None:
            bx, by, bw, bh = box
            ax = bx + bw - pad
            ay = by + bh // 2
            draw.text((ax, ay), text, font=font, fill=fill, anchor="rm")

        center_in_box(month_s, month_font, month_box, _W75)
        center_in_box(date_s, date_font, date_box, _W75)
        right_align_in_box(time_s, time_font, time_box, _W100)
        center_in_box(ampm_s, ampm_font, ampm_box, _W100)
        center_in_box(dow_s, day_font, day_box, _W75)
        return img

    def _rgba_clock_image(self, w: int, h: int) -> Image.Image:
        """
        Full 5×2 footprint ``w``×``h`` (grid origin unchanged). Content is drawn at
        ``_CLOCK_CONTENT_SCALE`` size and pasted at the top-left so cell [anchor] stays the origin.
        """
        cw = max(1, int(round(w * _CLOCK_CONTENT_SCALE)))
        ch = max(1, int(round(h * _CLOCK_CONTENT_SCALE)))
        content = self._rgba_clock_content(cw, ch)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.paste(content, (0, 0), content)
        return canvas

    def bgra_patch(self) -> np.ndarray:
        """Clock only, transparent background; shape (h, w, 4) BGRA at design scale for this widget."""
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=self._anchor_row,
            col_1based=self._anchor_col,
        )
        rgba = np.asarray(self._rgba_clock_image(w, h))
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)

    def render(self, canvas_bgr: np.ndarray) -> None:
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=self._anchor_row,
            col_1based=self._anchor_col,
        )
        roi = canvas_bgr[wy : wy + h, wx : wx + w]
        rgba = np.asarray(self._rgba_clock_image(w, h))
        bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
        roi[:] = alpha_blend_bgra_over_bgr(roi, bgra)
