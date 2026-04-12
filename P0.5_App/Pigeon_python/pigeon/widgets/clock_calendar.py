"""Clock widget: 5×2 squares on the global 19×8 grid.

Anchor the 5×2 span by **top-left** (default row 1, cols 11–15): glyphs are right-aligned inside the span so
``am``/``pm`` sits near the span’s right edge (adjacent to the app badge to the right when using Pigeon defaults).

Layout: one line — ``HH:MM`` (bold) + `` am|pm`` (medium). The line stays in **grid row 1** only
(top half of the 5×2 span), with padding from the top of that row so glyphs do not spill into row 2.

A subtle drop shadow uses the same BGR accent as the now-playing progress bar (``set_shadow_accent_bgr``).

Text is **right-aligned** within the full grid footprint.
"""

from __future__ import annotations

import os
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr, scale_bgra_rgb
from pigeon.design import rect_for_span_at_cell
from pigeon.font_paths import resolve_ui_font_bold, resolve_ui_font_medium

_SPAN = (5, 2)

# Slightly smaller than the full 5×2 footprint; block stays right-aligned in the span.
_CLOCK_CONTENT_SCALE = 0.76
# Keep glyphs inside grid **row 1** only (widget spans two rows); breathing room from top of row 1.
_CLOCK_ROW1_TOP_PAD_FRAC = 0.14
_CLOCK_ROW1_BOTTOM_MARGIN = 3

_DEFAULT_SHADOW_ACCENT_BGR = (40, 140, 255)

DEFAULT_FAKE_TIME_STRING = "2020-11-30 12:55:00"

_TEXT_WHITE = (255, 255, 255, 255)
_SHADOW_ALPHA = 110
def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return r - l, b - t


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _time_12h_no_leading_zero(now: datetime) -> str:
    h12 = now.hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{now.strftime('%M')}"


def _resolve_display_time() -> datetime:
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


def _fit_segmented_fonts(
    medium_path: str | None,
    bold_path: str,
    left: str,
    mid: str,
    right: str,
    draw: ImageDraw.ImageDraw,
    max_w: int,
    max_h: int,
    min_sz: int = 6,
) -> tuple[ImageFont.ImageFont, ImageFont.ImageFont]:
    """One point size; ``mid`` uses bold, ``left``/``right`` use medium (or bold path if no medium)."""
    med_p = medium_path if medium_path else bold_path
    lo, hi = min_sz, max(max_h * 3, max_w, 120)
    best_pair: tuple[ImageFont.ImageFont, ImageFont.ImageFont] | None = None
    while lo <= hi:
        mid_sz = (lo + hi) // 2
        f_med = _load_font(med_p, mid_sz)
        f_bold = _load_font(bold_path, mid_sz)
        tw = (
            (_text_size(draw, left, f_med)[0] if left else 0)
            + _text_size(draw, mid, f_bold)[0]
            + _text_size(draw, right, f_med)[0]
        )
        th = max(
            _text_size(draw, left, f_med)[1] if left else 0,
            _text_size(draw, mid, f_bold)[1],
            _text_size(draw, right, f_med)[1],
        )
        if tw <= max_w and th <= max_h:
            best_pair = (f_med, f_bold)
            lo = mid_sz + 1
        else:
            hi = mid_sz - 1
    if best_pair is None:
        sz = min_sz
        return _load_font(med_p, sz), _load_font(bold_path, sz)
    return best_pair


def design_clock_calendar_medium_font_point_size(
    *,
    anchor_row: int = 1,
    anchor_col: int = 11,
) -> int:
    """
    PIL truetype point size for the **medium** face after fitting the clock line in the
    5×2 patch — same rules as ``ClockCalendarWidget`` (time + am/pm).

    Default anchor matches ``pigeon_0_5.CLOCK_ANCHOR_ROW`` / ``CLOCK_ANCHOR_COL``.
    """
    _wx, _wy, w0, h0 = rect_for_span_at_cell(
        _SPAN[0],
        _SPAN[1],
        row_1based=anchor_row,
        col_1based=anchor_col,
    )
    w = max(1, int(round(w0 * _CLOCK_CONTENT_SCALE)))
    h = max(1, int(round(h0 * _CLOCK_CONTENT_SCALE)))
    now = _resolve_display_time()
    left = ""
    mid = _time_12h_no_leading_zero(now)
    right = f" {now.strftime('%p').lower()}"

    bold_path = resolve_ui_font_bold()
    if not bold_path:
        bold_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    medium_path = resolve_ui_font_medium()
    if not medium_path:
        medium_path = "/System/Library/Fonts/Supplemental/Arial.ttf"

    pad = max(2, int(min(w, h) * 0.04))
    inner_w = max(1, w - 2 * pad)
    row1_h = max(1, h // 2)
    top_m = max(2, int(round(row1_h * _CLOCK_ROW1_TOP_PAD_FRAC)))
    bot_m = max(2, _CLOCK_ROW1_BOTTOM_MARGIN)
    inner_h_fit = max(6, row1_h - top_m - bot_m - max(2, int(round(pad * 0.75))))

    scratch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    f_med, _f_bold = _fit_segmented_fonts(
        medium_path, bold_path, left, mid, right, draw, inner_w, inner_h_fit
    )
    sz = getattr(f_med, "size", None)
    if isinstance(sz, (int, float)) and int(sz) > 0:
        return int(sz)
    return 12


def design_clock_text_left_inset_in_span(
    *,
    anchor_row: int = 1,
    anchor_col: int = 11,
) -> int:
    """
    Pixels from the **left edge of the clock’s 5×2 span** to the **left edge of the time glyphs**
    (matches ``ClockCalendarWidget`` layout: scaled block right-aligned in the span). Use to align
    other widgets (e.g. location toast) with the clock text.     Defaults match ``CLOCK_ANCHOR_ROW`` / ``CLOCK_ANCHOR_COL`` in ``pigeon_0_5``.
    """
    _wx, _wy, w0, h0 = rect_for_span_at_cell(
        _SPAN[0],
        _SPAN[1],
        row_1based=anchor_row,
        col_1based=anchor_col,
    )
    cw = max(1, int(round(w0 * _CLOCK_CONTENT_SCALE)))
    ch = max(1, int(round(h0 * _CLOCK_CONTENT_SCALE)))
    now = _resolve_display_time()
    left = ""
    mid = _time_12h_no_leading_zero(now)
    right = f" {now.strftime('%p').lower()}"

    bold_path = resolve_ui_font_bold()
    if not bold_path:
        bold_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    medium_path = resolve_ui_font_medium()
    if not medium_path:
        medium_path = "/System/Library/Fonts/Supplemental/Arial.ttf"

    pad = max(2, int(min(cw, ch) * 0.04))
    inner_w = max(1, cw - 2 * pad)
    row1_h = max(1, ch // 2)
    top_m = max(2, int(round(row1_h * _CLOCK_ROW1_TOP_PAD_FRAC)))
    bot_m = max(2, _CLOCK_ROW1_BOTTOM_MARGIN)
    inner_h_fit = max(6, row1_h - top_m - bot_m - max(2, int(round(pad * 0.75))))

    scratch = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scratch)
    f_med, f_bold = _fit_segmented_fonts(
        medium_path, bold_path, left, mid, right, draw, inner_w, inner_h_fit
    )
    total_w = (
        (_text_size(draw, left, f_med)[0] if left else 0)
        + _text_size(draw, mid, f_bold)[0]
        + _text_size(draw, right, f_med)[0]
    )
    x0 = max(pad, cw - pad - total_w)
    return (w0 - cw) + x0


class ClockCalendarWidget:
    def __init__(self, *, anchor_row: int = 1, anchor_col: int = 1) -> None:
        self._anchor_row = anchor_row
        self._anchor_col = anchor_col
        self._shadow_accent_bgr: tuple[int, int, int] = _DEFAULT_SHADOW_ACCENT_BGR

    @property
    def grid_span(self) -> tuple[int, int]:
        return _SPAN

    @property
    def grid_anchor(self) -> tuple[int, int]:
        return (self._anchor_row, self._anchor_col)

    def set_shadow_accent_bgr(self, bgr: tuple[int, int, int] | None) -> None:
        if bgr is None or len(bgr) != 3:
            self._shadow_accent_bgr = _DEFAULT_SHADOW_ACCENT_BGR
            return
        self._shadow_accent_bgr = (int(bgr[0]), int(bgr[1]), int(bgr[2]))

    def _clock_segments(self, now: datetime) -> tuple[str, str, str]:
        time_s = _time_12h_no_leading_zero(now)
        ampm_s = now.strftime("%p").lower()
        left = ""
        mid = time_s
        right = f" {ampm_s}"
        return left, mid, right

    def _rgba_clock_content(self, w: int, h: int) -> Image.Image:
        now = _resolve_display_time()
        left, mid, right = self._clock_segments(now)

        bold_path = resolve_ui_font_bold()
        if not bold_path:
            bold_path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        medium_path = resolve_ui_font_medium()
        if not medium_path:
            medium_path = "/System/Library/Fonts/Supplemental/Arial.ttf"

        pad = max(2, int(min(w, h) * 0.04))
        inner_w = max(1, w - 2 * pad)
        row1_h = max(1, h // 2)
        top_m = max(2, int(round(row1_h * _CLOCK_ROW1_TOP_PAD_FRAC)))
        bot_m = max(2, _CLOCK_ROW1_BOTTOM_MARGIN)
        inner_h_fit = max(6, row1_h - top_m - bot_m - max(2, int(round(pad * 0.75))))
        band_top = top_m
        band_bot = row1_h - bot_m

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        f_med, f_bold = _fit_segmented_fonts(
            medium_path, bold_path, left, mid, right, draw, inner_w, inner_h_fit
        )

        total_w = (
            (_text_size(draw, left, f_med)[0] if left else 0)
            + _text_size(draw, mid, f_bold)[0]
            + _text_size(draw, right, f_med)[0]
        )
        x0 = max(pad, w - pad - total_w)
        # Vertically center in grid row 1 between top padding and bottom margin (never into row 2).
        band_mid = (band_top + band_bot) // 2
        baseline_y = band_mid

        def _line_bbox_at(by: int) -> tuple[int, int, int, int]:
            xr = x0
            ul = ut = 999999
            dr = db = -999999
            for text, font in ((left, f_med), (mid, f_bold), (right, f_med)):
                if not text:
                    continue
                bb = draw.textbbox((xr, by), text, font=font, anchor="ls")
                ul, ut = min(ul, bb[0]), min(ut, bb[1])
                dr, db = max(dr, bb[2]), max(db, bb[3])
                xr = bb[2]
            return ul, ut, dr, db

        ul, ut, dr, db = _line_bbox_at(baseline_y)
        cy = (ut + db) // 2
        baseline_y += band_mid - cy
        ul, ut, dr, db = _line_bbox_at(baseline_y)
        if db > band_bot:
            baseline_y -= db - band_bot
        if ut < band_top:
            baseline_y += band_top - ut

        b_bgr, g_bgr, r_bgr = self._shadow_accent_bgr
        shadow_rgb = (int(r_bgr), int(g_bgr), int(b_bgr))
        shadow_fill = shadow_rgb + (_SHADOW_ALPHA,)
        off = max(1, int(round(min(w, h) * 0.018)))

        x = x0
        for text, font in ((left, f_med), (mid, f_bold), (right, f_med)):
            if not text:
                continue
            draw.text((x + off, baseline_y + off), text, font=font, fill=shadow_fill, anchor="ls")
            draw.text((x, baseline_y), text, font=font, fill=_TEXT_WHITE, anchor="ls")
            bx = draw.textbbox((x, baseline_y), text, font=font, anchor="ls")
            x = bx[2]
        return img

    def _rgba_clock_image(self, w: int, h: int) -> Image.Image:
        cw = max(1, int(round(w * _CLOCK_CONTENT_SCALE)))
        ch = max(1, int(round(h * _CLOCK_CONTENT_SCALE)))
        content = self._rgba_clock_content(cw, ch)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        # Right-align the glyph block: trailing segment (am/pm) hugs the span’s right edge.
        canvas.paste(content, (w - cw, 0), content)
        return canvas

    def bgra_patch(self) -> np.ndarray:
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=self._anchor_row,
            col_1based=self._anchor_col,
        )
        rgba = np.asarray(self._rgba_clock_image(w, h))
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)

    def render(self, canvas_bgr: np.ndarray, *, rgb_scale: float = 1.0) -> None:
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=self._anchor_row,
            col_1based=self._anchor_col,
        )
        roi = canvas_bgr[wy : wy + h, wx : wx + w]
        rgba = np.asarray(self._rgba_clock_image(w, h))
        bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
        if rgb_scale < 0.999:
            bgra = scale_bgra_rgb(bgra, rgb_scale)
        roi[:] = alpha_blend_bgra_over_bgr(roi, bgra)
