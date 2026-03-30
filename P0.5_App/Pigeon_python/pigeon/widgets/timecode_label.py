"""Static timecode-style label on the global 19×8 grid (configurable string, e.g. ``0:00:00``).

Occupies a horizontal band: top-left anchor ``[row, col]``, span ``(cols_wide, rows_tall)``.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.design import rect_for_span_at_cell
from pigeon.font_paths import resolve_ui_font_bold, resolve_ui_font_medium

_SPAN = (3, 1)
_FILL = (255, 255, 255, 255)
# Tight inset so glyphs nearly fill the three grid squares (still avoids edge clipping).
_PAD_FRAC = 0.04


def _pick_font_path() -> str:
    p = resolve_ui_font_medium()
    if p:
        return p
    p = resolve_ui_font_bold()
    if p:
        return p
    return "/System/Library/Fonts/Supplemental/Arial.ttf"


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _text_size(font: ImageFont.ImageFont, text: str) -> tuple[int, int]:
    if hasattr(font, "getbbox"):
        l, t, r, b = font.getbbox(text)
        return r - l, b - t
    w, h = font.getsize(text)
    return w, h


def _fit_font_to_box(path: str, text: str, max_w: int, max_h: int, min_sz: int = 6) -> ImageFont.ImageFont:
    """Largest font size so ``text`` fits in ``max_w`` × ``max_h``."""
    if max_w < 4 or max_h < 4:
        return _load_font(path, min_sz)
    lo, hi = min_sz, max(max_h * 4, max_w * 2, 400)
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
    return best


class TimecodeLabelWidget:
    """Timecode-style string in Sharp Sans Medium, scaled to nearly fill the 3×1 cell span."""

    def __init__(
        self,
        *,
        anchor_row: int = 1,
        anchor_col: int = 7,
        text: str = "0:00:00",
    ) -> None:
        self._anchor_row = anchor_row
        self._anchor_col = anchor_col
        self._text = text

    @property
    def grid_span(self) -> tuple[int, int]:
        return _SPAN

    @property
    def grid_anchor(self) -> tuple[int, int]:
        return (self._anchor_row, self._anchor_col)

    def _rgba_image(self, w: int, h: int) -> Image.Image:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pad = max(2, int(round(min(w, h) * _PAD_FRAC)))
        mw = max(4, w - 2 * pad)
        mh = max(4, h - 2 * pad)
        font_path = _pick_font_path()
        font = _fit_font_to_box(font_path, self._text, mw, mh)
        l, t, r, b = draw.textbbox((0, 0), self._text, font=font)
        tw, th = r - l, b - t
        cx, cy = w // 2, h // 2
        x = cx - (tw // 2) - l
        y = cy - (th // 2) - t
        draw.text((x, y), self._text, font=font, fill=_FILL)
        return img

    def bgra_patch(self) -> np.ndarray:
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=self._anchor_row,
            col_1based=self._anchor_col,
        )
        rgba = np.asarray(self._rgba_image(w, h))
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)

    def render(self, canvas_bgr: np.ndarray) -> None:
        wx, wy, w, h = rect_for_span_at_cell(
            _SPAN[0],
            _SPAN[1],
            row_1based=self._anchor_row,
            col_1based=self._anchor_col,
        )
        roi = canvas_bgr[wy : wy + h, wx : wx + w]
        rgba = np.asarray(self._rgba_image(w, h))
        bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
        roi[:] = alpha_blend_bgra_over_bgr(roi, bgra)
