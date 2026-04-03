"""Clock + calendar widget: 5×2 squares on the global 19×8 grid.

Anchor so the span is **cols 13–17** on row 1 (default): trailing ``am``/``pm`` sits in column 17, right-aligned,
so the block is not clipped past the design canvas edge.

Layout: one line — ``month day`` (medium) + ``time`` (bold) + `` am|pm`` (medium), spaces only (no slashes).
The date segment has a black outline; there is no background pill. The line is vertically centered in **row 1**
of the 5×2 span (top grid row only).

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

# Use the full 5×2 footprint so right-aligned text (am/pm in col 17) is not squeezed off-canvas.
_CLOCK_CONTENT_SCALE = 1.0

_DEFAULT_SHADOW_ACCENT_BGR = (40, 140, 255)

DEFAULT_FAKE_TIME_STRING = "2020-11-30 12:55:00"

_TEXT_WHITE = (255, 255, 255, 255)
_SHADOW_ALPHA = 110
# Calendar (month + day) white text with black outline for contrast without the old pill.
_CAL_STROKE_W = 2
_CAL_STROKE_RGBA = (0, 0, 0, 255)


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
            _text_size(draw, left, f_med)[0]
            + _text_size(draw, mid, f_bold)[0]
            + _text_size(draw, right, f_med)[0]
        )
        th = max(
            _text_size(draw, left, f_med)[1],
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
        month_s = now.strftime("%B").lower()
        date_s = str(now.day)
        time_s = _time_12h_no_leading_zero(now)
        ampm_s = now.strftime("%p").lower()
        # Single spaces between month/day and before am/pm; two spaces between date and time.
        left = f"{month_s} {date_s}  "
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
        inner_h = max(1, h - 2 * pad)

        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        f_med, f_bold = _fit_segmented_fonts(
            medium_path, bold_path, left, mid, right, draw, inner_w, inner_h
        )

        total_w = (
            _text_size(draw, left, f_med)[0]
            + _text_size(draw, mid, f_bold)[0]
            + _text_size(draw, right, f_med)[0]
        )
        x0 = max(pad, w - pad - total_w)
        # Vertically center the line in **grid row 1** only (this widget spans two rows).
        row1_h = max(1, h // 2)
        y_center_row1 = row1_h // 2
        baseline_y = row1_h // 2

        def _line_bbox_at(by: int) -> tuple[int, int, int, int]:
            xr = x0
            ul = ut = 999999
            dr = db = -999999
            for text, font in ((left, f_med), (mid, f_bold), (right, f_med)):
                bb = draw.textbbox((xr, by), text, font=font, anchor="ls")
                ul, ut = min(ul, bb[0]), min(ut, bb[1])
                dr, db = max(dr, bb[2]), max(db, bb[3])
                xr = bb[2]
            return ul, ut, dr, db

        ul, ut, dr, db = _line_bbox_at(baseline_y)
        cy = (ut + db) // 2
        baseline_y += y_center_row1 - cy

        b_bgr, g_bgr, r_bgr = self._shadow_accent_bgr
        shadow_rgb = (int(r_bgr), int(g_bgr), int(b_bgr))
        shadow_fill = shadow_rgb + (_SHADOW_ALPHA,)
        off = max(1, int(round(min(w, h) * 0.018)))

        x = x0
        for idx, (text, font) in enumerate(((left, f_med), (mid, f_bold), (right, f_med))):
            draw.text((x + off, baseline_y + off), text, font=font, fill=shadow_fill, anchor="ls")
            if idx == 0:
                draw.text(
                    (x, baseline_y),
                    text,
                    font=font,
                    fill=_TEXT_WHITE,
                    anchor="ls",
                    stroke_width=_CAL_STROKE_W,
                    stroke_fill=_CAL_STROKE_RGBA,
                )
            else:
                draw.text((x, baseline_y), text, font=font, fill=_TEXT_WHITE, anchor="ls")
            bx = draw.textbbox((x, baseline_y), text, font=font, anchor="ls")
            x = bx[2]
        return img

    def _rgba_clock_image(self, w: int, h: int) -> Image.Image:
        cw = max(1, int(round(w * _CLOCK_CONTENT_SCALE)))
        ch = max(1, int(round(h * _CLOCK_CONTENT_SCALE)))
        content = self._rgba_clock_content(cw, ch)
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        # Right-align the glyph block: trailing segment (am/pm) hugs the span’s right edge (col 17 when anchored at 13).
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
