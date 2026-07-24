"""
Pigeon 0.8 ``view_circles`` now-playing skin (800×480).

Static chrome is rasterized from ``pigeonAssets/view_circles.svg``. Dynamic layers
(cast, clock, volume, progress rings, status bar, poster art) are drawn on top.
"""

from __future__ import annotations

import io
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr, cv_resize_interp
from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.font_paths import resolve_digital7_font
from pigeon.widgets.playback_overlay import (
    _receiver_volume_display_line,
    receiver_audio_config_display_line,
    volume_fraction_from_display_line,
)
from pigeon.widgets.search_spinner import (
    advance_angle_deg,
    blit_spinner_patch,
    build_search_spinner_frames,
    rotated_patch_for_angle,
)

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

_SVG_W = 800.0
_SVG_H = 480.0

_COLOR_BG_HEX = "#000000"
_COLOR_ACCENT_BGR = (0, 0, 255)  # #FF0000
_COLOR_UNPLAYED_BGR = (147, 147, 147)  # #939393
_COLOR_BUTTON_BGR = (35, 35, 35)  # #232323
# Strokes, CTI, Digital-7 labels — gray accents (was pure white).
_COLOR_CHROME_BGR = (147, 147, 147)  # #939393
_COLOR_CHROME_RGB = (147, 147, 147)

# Geometry from ``view_circles.svg`` (design coords = SVG 800×480).
_RING_OUTER_R = 114.26
_RING_INNER_R = 93.54
_CIRCLE1_CX = 152.958
_CIRCLE1_CY = 158.37
_CIRCLE2_CX = 646.958
_CIRCLE2_CY = 158.37

_POSTER_X = 300
_POSTER_Y = 23
_POSTER_W = 200
_POSTER_H = 300
_POSTER_RX = 10

_BAR_L = 76
_BAR_R = 724
_BAR_T = 408
_BAR_H = 25
_BAR_RX = 8
_BAR_W = _BAR_R - _BAR_L
_BAR_CENTER_Y = _BAR_T + _BAR_H / 2.0
_CTI_W = 8
# Tall enough to read on the bar, short enough to clear elapsed text below.
_CTI_OVERHANG_TOP = 10
_CTI_OVERHANG_BOTTOM = 2
_CTI_H = _BAR_H + _CTI_OVERHANG_TOP + _CTI_OVERHANG_BOTTOM
_CTI_Y = _BAR_T - _CTI_OVERHANG_TOP
_MIN_ELAPSED_W = 4  # one column of pixels
_ELAPSED_REMAINING_GAP_PX = 16

# Text anchors (SVG matrix translations ≈ baseline-ish; we center/align programmatically).
_DATE_CX, _DATE_CY = 152.958, 128.0
_TIME_CX, _TIME_CY = 152.958, 168.0
_REMAINING_TIME_CX, _REMAINING_TIME_CY = 152.958, 300.0
_VOLUME_CX, _VOLUME_CY = 646.958, 150.0
_AUDIO_CFG_CX, _AUDIO_CFG_CY = 646.958, 188.0

# Cast columns: shared horizontal center for actor + character in each column.
_CAST_COLS: tuple[tuple[float, int, int], ...] = (
    # (center_x, actor_y, character_y)
    (152.958, 344, 362),
    (400.0, 344, 362),
    (646.958, 344, 362),
)
_CAST_COL_W = 220

_ELAPSED_TEXT_Y = 448
_REMAINING_TEXT_Y = 448

# Dynamic SVG layer ids (Illustrator-encoded) to strip before rasterize.
_STRIP_SVG_IDS: tuple[str, ...] = (
    "time_x5F_text",
    "date_x5F_text",
    "remaining_x5F_time_x5F_text",
    "volume_x5F_text",
    "audio_x5F_config_x5F_text",
    "elapsed_x5F_text",
    "remaining_x5F_text",
    "actor1_x5F_text",
    "character1_x5F_text",
    "actor2_x5F_text",
    "character2_x5F_text",
    "actor3_x5F_text",
    "character3_x5F_text",
    "poster_x5F_tmdb",
    "circle2_x5F_accent",
    "now_x5F_playing_x5F_played_x5F_icon",
    "now_x5F_playing_x5F_circle_x5F_group",
    "elapsed_x5F_icon",
    "cti_x5F_icon",
    # Demo remaining bar is full-width grey; we redraw it with elapsed overlay.
    "remainikng_x5F_icon",
    # Circle chrome redrawn programmatically so both rings share one pipeline.
    "clock_x5F_button",
    "circle2_x5F_button",
    "circle2_x5F_volume_x5F_headroom_x5F_buton",
    "circle1_x5F_botton_00000082351325665415078510000008606994160936758171_",
)


@dataclass
class ViewCirclesState:
    progress: float = 0.0
    elapsed_text: str = ""
    remaining_text: str = ""
    volume: str = ""
    volume_fraction: float = 0.0
    volume_muted: bool = False
    incoming: str = ""
    config: str = ""
    chrome_visible: bool = False
    cast: list[tuple[str, str]] = field(default_factory=list)
    # True while a TMDb fetch is in flight — poster shows the searching spinner.
    searching: bool = False
    search_angle_deg: float = 0.0


def default_view_circles_svg_path(assets_dir: Path | str | None = None) -> Path:
    env = os.environ.get("PIGEON_VIEW_CIRCLES_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "view_circles.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "view_circles.svg"


def _find_by_id(root: ET.Element, layer_id: str) -> ET.Element | None:
    for el in root.iter():
        if el.get("id") == layer_id:
            return el
    return None


def _remove_element_by_id(root: ET.Element, element_id: str) -> None:
    for parent in root.iter():
        for child in list(parent):
            if child.get("id") == element_id:
                parent.remove(child)
                return


def _replace_background_with_black(root: ET.Element) -> None:
    el = _find_by_id(root, "background")
    if el is None:
        return
    for node in el.iter():
        style = node.get("style") or ""
        if "fill:" in style:
            node.set("style", re.sub(r"fill:[^;\"']+", f"fill:{_COLOR_BG_HEX}", style))
        if node.get("fill"):
            node.set("fill", _COLOR_BG_HEX)


def _svg_tree_from_path(path: Path) -> ET.Element:
    tree = ET.parse(path)
    root = tree.getroot()
    root.set("viewBox", f"0 0 {int(_SVG_W)} {int(_SVG_H)}")
    root.set("width", str(int(_SVG_W)))
    root.set("height", str(int(_SVG_H)))
    return root


def _scale_raster_to_design(bgra: np.ndarray, src_w: int, src_h: int) -> np.ndarray:
    if bgra.shape[0] != src_h or bgra.shape[1] != src_w:
        bgra = cv2.resize(bgra, (src_w, src_h), interpolation=cv2.INTER_AREA)
    return cv2.resize(bgra, (int(DESIGN_W), int(DESIGN_H)), interpolation=cv2.INTER_AREA)


def _rasterize_svg_tree(root: ET.Element) -> np.ndarray:
    svg_bytes = ET.tostring(root, encoding="utf-8")
    src_w, src_h = int(_SVG_W), int(_SVG_H)
    last_err: Exception | None = None

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=svg_bytes, filetype="svg")
        page = doc[0]
        pix = page.get_pixmap(
            matrix=fitz.Matrix(src_w / page.rect.width, src_h / page.rect.height)
        )
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGBA2BGRA)
        else:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGRA)
        return _scale_raster_to_design(bgra, src_w, src_h)
    except ImportError as exc:
        last_err = exc
    except Exception as exc:
        last_err = exc

    try:
        import cairosvg

        out = io.BytesIO()
        cairosvg.svg2png(
            bytestring=svg_bytes,
            write_to=out,
            output_width=src_w,
            output_height=src_h,
        )
        data = np.frombuffer(out.getvalue(), dtype=np.uint8)
        raw = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if raw is None:
            raise RuntimeError("SVG raster decode failed")
        if raw.ndim == 2:
            bgra = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGRA)
        elif raw.shape[2] == 3:
            bgra = cv2.cvtColor(raw, cv2.COLOR_BGR2BGRA)
        else:
            bgra = raw
        return _scale_raster_to_design(bgra, src_w, src_h)
    except ImportError as exc:
        last_err = exc
    except OSError as exc:
        last_err = exc
    except Exception as exc:
        last_err = exc

    msg = "view_circles needs PyMuPDF (pip install pymupdf) or cairosvg with system cairo."
    if last_err is not None:
        raise RuntimeError(msg) from last_err
    raise RuntimeError(msg)


def apply_view_circles_svg_state(root: ET.Element) -> None:
    root.set("style", f"background:{_COLOR_BG_HEX}")
    _replace_background_with_black(root)
    for element_id in _STRIP_SVG_IDS:
        _remove_element_by_id(root, element_id)


def render_view_circles_svg_base_bgra(
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    if svg_path is not None:
        path = Path(svg_path)
    else:
        path = default_view_circles_svg_path(assets_dir)
    if not path.is_file():
        raise FileNotFoundError(f"view_circles SVG not found: {path}")
    root = _svg_tree_from_path(path)
    apply_view_circles_svg_state(root)
    return _rasterize_svg_tree(root)


@lru_cache(maxsize=32)
def _load_digital7(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    px = max(6, int(size))
    path = resolve_digital7_font()
    candidates: list[str] = []
    if path:
        candidates.append(str(path))
    for fallback in (
        "/usr/share/fonts/truetype/digital-7/digital-7.ttf",
        "/Library/Fonts/Digital-7.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if fallback not in candidates:
            candidates.append(fallback)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_patch_digital7(
    text: str,
    *,
    size_px: int,
    fill_rgb: tuple[int, int, int] = _COLOR_CHROME_RGB,
    max_width_px: int | None = None,
) -> tuple[np.ndarray, int, int]:
    draw_text = str(text or "")
    if not draw_text:
        return np.zeros((1, 1, 4), dtype=np.uint8), 0, 0
    font = _load_digital7(size_px)
    pad = 2
    probe = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)

    def _measure(s: str) -> tuple[int, int, int, int]:
        return draw.textbbox((0, 0), s, font=font)

    if max_width_px is not None and max_width_px > 0:
        l, t, r, b = _measure(draw_text)
        if (r - l) > max_width_px:
            ell = "…"
            for n in range(len(draw_text), 0, -1):
                candidate = draw_text[:n].rstrip() + ell
                l2, t2, r2, b2 = _measure(candidate)
                if (r2 - l2) <= max_width_px:
                    draw_text = candidate
                    break

    l, t, r, b = _measure(draw_text)
    tw, th = max(1, r - l), max(1, b - t)
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((pad - l, pad - t), draw_text, font=font, fill=(*fill_rgb, 255))
    arr = np.asarray(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA), tw + pad * 2, th + pad * 2


def _paste_patch_bgra(canvas: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
    if patch is None or patch.size == 0 or canvas is None or canvas.size == 0:
        return
    ph, pw = patch.shape[:2]
    if pw < 1 or ph < 1:
        return
    x0 = int(x)
    y0 = int(y)
    x1 = x0 + pw
    y1 = y0 + ph
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1 = min(int(canvas.shape[1]), x1)
    cy1 = min(int(canvas.shape[0]), y1)
    if cx0 >= cx1 or cy0 >= cy1:
        return
    sx0 = cx0 - x0
    sy0 = cy0 - y0
    src = patch[sy0 : sy0 + (cy1 - cy0), sx0 : sx0 + (cx1 - cx0)]
    roi = canvas[cy0:cy1, cx0:cx1]
    if roi.ndim == 3 and roi.shape[2] == 3:
        roi[:] = alpha_blend_bgra_over_bgr(roi, src)
    elif roi.ndim == 3 and roi.shape[2] >= 4:
        blended = alpha_blend_bgra_over_bgr(roi[:, :, :3], src)
        roi[:, :, :3] = blended
        roi[:, :, 3] = np.maximum(roi[:, :, 3], src[:, :, 3])


def _paste_centered(canvas: np.ndarray, patch: np.ndarray, cx: float, cy: float) -> None:
    if patch is None or patch.size == 0:
        return
    ph, pw = patch.shape[:2]
    _paste_patch_bgra(canvas, patch, int(round(cx - pw / 2.0)), int(round(cy - ph / 2.0)))


def _rounded_rect_mask(w: int, h: int, radius: int) -> np.ndarray:
    if w < 1 or h < 1:
        return np.zeros((max(0, h), max(0, w)), dtype=np.uint8)
    r = max(0, min(radius, min(w, h) // 2))
    mask = np.zeros((h, w), dtype=np.uint8)
    if r <= 0:
        mask[:, :] = 255
        return mask
    cv2.rectangle(mask, (r, 0), (w - r - 1, h - 1), 255, -1)
    cv2.rectangle(mask, (0, r), (w - 1, h - r - 1), 255, -1)
    cv2.circle(mask, (r, r), r, 255, -1, lineType=cv2.LINE_AA)
    cv2.circle(mask, (w - r - 1, r), r, 255, -1, lineType=cv2.LINE_AA)
    cv2.circle(mask, (r, h - r - 1), r, 255, -1, lineType=cv2.LINE_AA)
    cv2.circle(mask, (w - r - 1, h - r - 1), r, 255, -1, lineType=cv2.LINE_AA)
    return mask


def _draw_rounded_bar_bgra(
    bgra: np.ndarray,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    fill_bgr: tuple[int, int, int],
    radius: int,
    stroke_bgr: tuple[int, int, int] | None = None,
    stroke: int = 2,
) -> None:
    if w < 1 or h < 1:
        return
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(DESIGN_W), x + w), min(int(DESIGN_H), y + h)
    if x0 >= x1 or y0 >= y1:
        return
    lw, lh = x1 - x0, y1 - y0
    # Pad so the outer stroke is not clipped at the patch edge.
    pad = max(0, int(stroke) + 1) if stroke_bgr is not None and stroke > 0 else 0
    mw, mh = lw + pad * 2, lh + pad * 2
    mask = np.zeros((mh, mw), dtype=np.uint8)
    inner = _rounded_rect_mask(lw, lh, min(radius, lw // 2, lh // 2))
    mask[pad : pad + lh, pad : pad + lw] = inner
    patch = np.zeros((mh, mw, 4), dtype=np.uint8)
    patch[mask > 0, :3] = fill_bgr
    patch[mask > 0, 3] = 255
    if stroke_bgr is not None and stroke > 0:
        # Full perimeter stroke (not just the mask edge after erode — that missed ends).
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cv2.drawContours(
                patch,
                contours,
                -1,
                (*stroke_bgr, 255),
                thickness=max(1, int(stroke)),
                lineType=cv2.LINE_AA,
            )
    _paste_patch_bgra(bgra, patch, x0 - pad, y0 - pad)


def annular_sector_mask(
    size: int,
    *,
    outer_r: float,
    inner_r: float,
    fraction: float,
    start_deg: float = -90.0,
) -> np.ndarray:
    """
    Annular pie mask. ``fraction`` 0..1 grows clockwise from 12-o'clock
    (OpenCV ellipse angles: 0° = 3-o'clock, -90° = 12-o'clock).
    """
    s = max(1, int(size))
    mask = np.zeros((s, s), dtype=np.uint8)
    frac = max(0.0, min(1.0, float(fraction)))
    if frac <= 1e-6:
        return mask
    cx_i = (s - 1) // 2
    cy_i = (s - 1) // 2
    outer = max(1, int(round(outer_r)))
    inner = max(0, min(int(round(inner_r)), outer - 1))
    # Full ring, then keep only the clockwise wedge from 12-o'clock.
    cv2.circle(mask, (cx_i, cy_i), outer, 255, -1, lineType=cv2.LINE_AA)
    if inner > 0:
        cv2.circle(mask, (cx_i, cy_i), inner, 0, -1, lineType=cv2.LINE_AA)
    if frac >= 0.999:
        return mask
    sweep = 360.0 * frac
    start = float(start_deg)
    end = start + sweep
    # Wedge from center through the outer arc (fillPoly handles >180°).
    arc = cv2.ellipse2Poly(
        (cx_i, cy_i),
        (outer + 2, outer + 2),
        0,
        int(round(start)),
        int(round(end)),
        1,
    )
    wedge = np.zeros((s, s), dtype=np.uint8)
    poly = np.vstack([[[cx_i, cy_i]], arc])
    cv2.fillPoly(wedge, [poly], 255)
    return cv2.bitwise_and(mask, wedge)


def _draw_filled_circle_bgra(
    bgra: np.ndarray,
    *,
    cx: float,
    cy: float,
    r: float,
    fill_bgr: tuple[int, int, int],
    stroke_bgr: tuple[int, int, int] = _COLOR_CHROME_BGR,
    stroke: int = 2,
) -> None:
    radius = max(1, int(round(r)))
    pad = stroke + 2
    size = radius * 2 + pad * 2
    patch = np.zeros((size, size, 4), dtype=np.uint8)
    center = (size // 2, size // 2)
    cv2.circle(patch, center, radius, (*fill_bgr, 255), -1, lineType=cv2.LINE_AA)
    if stroke > 0:
        cv2.circle(patch, center, radius, (*stroke_bgr, 255), stroke, lineType=cv2.LINE_AA)
    x = int(round(cx - size / 2.0))
    y = int(round(cy - size / 2.0))
    _paste_patch_bgra(bgra, patch, x, y)


def _draw_progress_ring(
    bgra: np.ndarray,
    *,
    cx: float,
    cy: float,
    outer_r: float,
    inner_r: float,
    fraction: float,
    fill_bgr: tuple[int, int, int] = _COLOR_ACCENT_BGR,
    stroke_bgr: tuple[int, int, int] = _COLOR_CHROME_BGR,
    stroke: int = 2,
) -> None:
    """Annular pie from 12-o'clock, clockwise — shared by circle1 (progress) and circle2 (volume)."""
    frac = max(0.0, min(1.0, float(fraction)))
    if frac <= 1e-6:
        return
    pad = max(4, stroke + 2)
    size = int(math.ceil(outer_r * 2)) + pad * 2
    mask = annular_sector_mask(
        size,
        outer_r=outer_r,
        inner_r=inner_r,
        fraction=frac,
    )
    patch = np.zeros((size, size, 4), dtype=np.uint8)
    patch[mask > 0, :3] = fill_bgr
    patch[mask > 0, 3] = 255
    if stroke > 0:
        # Arc strokes (not full contour) — contour stroke left a gap at 12-o'clock on small pies.
        cx_i = cy_i = size // 2
        outer = max(1, int(round(outer_r)))
        inner = max(0, min(int(round(inner_r)), outer - 1))
        start = -90.0
        end = start + 360.0 * frac
        thick = max(1, int(stroke))
        color = (*stroke_bgr, 255)
        if frac >= 0.999:
            cv2.circle(patch, (cx_i, cy_i), outer, color, thick, lineType=cv2.LINE_AA)
            if inner > 0:
                cv2.circle(patch, (cx_i, cy_i), inner, color, thick, lineType=cv2.LINE_AA)
        else:
            cv2.ellipse(
                patch,
                (cx_i, cy_i),
                (outer, outer),
                0,
                start,
                end,
                color,
                thick,
                lineType=cv2.LINE_AA,
            )
            if inner > 0:
                cv2.ellipse(
                    patch,
                    (cx_i, cy_i),
                    (inner, inner),
                    0,
                    start,
                    end,
                    color,
                    thick,
                    lineType=cv2.LINE_AA,
                )
            # Radial edges at start (12-o'clock) and end of the sweep.
            for ang in (start, end):
                rad = math.radians(ang)
                # OpenCV angle: 0° along +x, clockwise positive in ellipse args…
                # cos/sin with image y-down: x = cos(θ), y = sin(θ) for CW-from-+x.
                x_o = int(round(cx_i + outer * math.cos(rad)))
                y_o = int(round(cy_i + outer * math.sin(rad)))
                x_i = int(round(cx_i + inner * math.cos(rad)))
                y_i = int(round(cy_i + inner * math.sin(rad)))
                cv2.line(patch, (x_i, y_i), (x_o, y_o), color, thick, lineType=cv2.LINE_AA)
    x = int(round(cx - size / 2.0))
    y = int(round(cy - size / 2.0))
    _paste_patch_bgra(bgra, patch, x, y)


def _draw_circle_pair(
    bgra: np.ndarray,
    *,
    cx: float,
    cy: float,
    fraction: float,
    show_accent: bool,
) -> None:
    """Headroom disc + optional pie accent + inner button (identical for circle1 / circle2)."""
    _draw_filled_circle_bgra(
        bgra,
        cx=cx,
        cy=cy,
        r=_RING_OUTER_R,
        fill_bgr=_COLOR_UNPLAYED_BGR,
    )
    if show_accent and fraction > 1e-6:
        _draw_progress_ring(
            bgra,
            cx=cx,
            cy=cy,
            outer_r=_RING_OUTER_R,
            inner_r=_RING_INNER_R,
            fraction=fraction,
        )
    _draw_filled_circle_bgra(
        bgra,
        cx=cx,
        cy=cy,
        r=_RING_INNER_R,
        fill_bgr=_COLOR_BUTTON_BGR,
    )


def _clock_hhmm_ampm(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now()
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.minute:02d} {ampm}"


def _date_mmddyy(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now()
    return f"{dt.month}/{dt.day}/{dt.strftime('%y')}"


def _fallback_base_bgra() -> np.ndarray:
    out = np.zeros((int(DESIGN_H), int(DESIGN_W), 4), dtype=np.uint8)
    out[:, :, 3] = 255
    return out


class ViewCirclesWidget:
    """Circles now-playing layout for DisplayView.ONE."""

    def __init__(self, *, assets_dir: Path) -> None:
        self._assets_dir = Path(assets_dir)
        self._state = ViewCirclesState()
        self._poster_bgra: np.ndarray | None = None
        self._cached_bgra: np.ndarray | None = None
        self._cached_sig: tuple[object, ...] | None = None
        self._svg_chrome_bgra: np.ndarray | None = None
        self._svg_chrome_sig: tuple[object, ...] | None = None
        self._search_frames: tuple[np.ndarray, ...] | None = None
        self._search_frames_tried = False
        self._last_tick_mono: float | None = None

    @property
    def chrome_visible(self) -> bool:
        return self._state.chrome_visible

    @property
    def searching(self) -> bool:
        return bool(self._state.searching)

    def clear_cache(self) -> None:
        self._cached_bgra = None
        self._cached_sig = None

    def set_now_playing_chrome_visible(self, visible: bool) -> bool:
        v = bool(visible)
        if v == self._state.chrome_visible:
            return False
        self._state.chrome_visible = v
        self.clear_cache()
        return True

    def set_poster_bgra(self, poster_bgra: np.ndarray | None) -> bool:
        if poster_bgra is None:
            if self._poster_bgra is None:
                return False
            self._poster_bgra = None
            self.clear_cache()
            return True
        arr = np.asarray(poster_bgra, dtype=np.uint8)
        if self._poster_bgra is not None and self._poster_bgra.shape == arr.shape:
            if np.array_equal(self._poster_bgra, arr):
                return False
        self._poster_bgra = arr.copy()
        self.clear_cache()
        return True

    def update_state(
        self,
        *,
        progress: float,
        elapsed_text: str,
        remaining_text: str,
        volume_text: str,
        volume_fraction: float | None = None,
        incoming_audio: str = "",
        playback_config: str = "",
        cast: list[tuple[str, str]] | None = None,
        poster_bgra: np.ndarray | None = None,
        has_now_playing: bool = True,
        searching: bool | None = None,
    ) -> bool:
        changed = False
        if self.set_now_playing_chrome_visible(has_now_playing):
            changed = True
        pf = max(0.0, min(1.0, float(progress)))
        if abs(pf - self._state.progress) > 1e-9:
            self._state.progress = pf
            changed = True
        et = str(elapsed_text or "")
        if et != self._state.elapsed_text:
            self._state.elapsed_text = et
            changed = True
        rt = str(remaining_text or "")
        if rt != self._state.remaining_text:
            self._state.remaining_text = rt
            changed = True
        vol = str(volume_text or "")
        if vol != self._state.volume:
            self._state.volume = vol
            changed = True
        muted = vol.strip().lower() in ("mute", "muted", "off")
        if muted != self._state.volume_muted:
            self._state.volume_muted = muted
            changed = True
        if volume_fraction is None:
            vf = volume_fraction_from_display_line(vol)
        else:
            vf = max(0.0, min(1.0, float(volume_fraction)))
        if abs(vf - self._state.volume_fraction) > 1e-6:
            self._state.volume_fraction = vf
            changed = True
        inc = str(incoming_audio or "")
        cfg = str(playback_config or "")
        if inc != self._state.incoming:
            self._state.incoming = inc
            changed = True
        if cfg != self._state.config:
            self._state.config = cfg
            changed = True
        if cast is not None:
            norm = [(str(a or ""), str(c or "")) for a, c in cast[:3]]
            while len(norm) < 3:
                norm.append(("", ""))
            if norm != self._state.cast:
                self._state.cast = norm
                changed = True
        if searching is not None:
            want = bool(searching)
            if want != self._state.searching:
                self._state.searching = want
                if want:
                    self._state.search_angle_deg = 0.0
                    self._last_tick_mono = None
                    self._ensure_search_frames()
                changed = True
        # While searching, keep the poster empty so the spinner is the only art.
        if self._state.searching:
            if self.set_poster_bgra(None):
                changed = True
        elif self.set_poster_bgra(poster_bgra):
            changed = True
        if changed:
            self.clear_cache()
        return changed

    def tick(self) -> None:
        """Advance searching spinner angle when a TMDb fetch is in flight."""
        if not self._state.searching:
            self._last_tick_mono = None
            return
        now = time.monotonic()
        if self._last_tick_mono is None:
            self._last_tick_mono = now
            return
        dt = max(0.0, now - self._last_tick_mono)
        self._last_tick_mono = now
        prev = self._state.search_angle_deg
        self._state.search_angle_deg = advance_angle_deg(prev, dt)
        # Quantize to spinner frame steps so we only invalidate when the blit changes.
        if int(round(prev / 10.0)) != int(round(self._state.search_angle_deg / 10.0)):
            self.clear_cache()

    def _ensure_search_frames(self) -> tuple[np.ndarray, ...] | None:
        if self._search_frames is not None:
            return self._search_frames
        if self._search_frames_tried:
            return None
        self._search_frames_tried = True
        self._search_frames = build_search_spinner_frames(self._assets_dir)
        return self._search_frames

    def _cache_sig(self) -> tuple[object, ...]:
        st = self._state
        cast_sig = tuple(st.cast[:3])
        poster_id = id(self._poster_bgra) if self._poster_bgra is not None else None
        now = datetime.now()
        search_frame = (
            int(round(st.search_angle_deg / 10.0)) % 36 if st.searching else -1
        )
        return (
            round(st.progress, 6),
            st.elapsed_text,
            st.remaining_text,
            st.volume,
            round(st.volume_fraction, 5),
            st.volume_muted,
            st.incoming,
            st.config,
            st.chrome_visible,
            cast_sig,
            poster_id,
            st.searching,
            search_frame,
            int(now.strftime("%H%M")),
            f"{now.month}/{now.day}/{now.strftime('%y')}",
        )

    def _svg_chrome_cache_sig(self) -> tuple[object, ...]:
        path = default_view_circles_svg_path(self._assets_dir)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = -1
        # Bump when strip/redraw pipeline changes so cached chrome is not reused.
        return (str(path), mtime, 3)

    def _render_svg_base(self) -> np.ndarray:
        sig = self._svg_chrome_cache_sig()
        if self._svg_chrome_bgra is not None and self._svg_chrome_sig == sig:
            return self._svg_chrome_bgra
        try:
            base = render_view_circles_svg_base_bgra(assets_dir=self._assets_dir)
        except Exception:
            base = _fallback_base_bgra()
        self._svg_chrome_bgra = base
        self._svg_chrome_sig = sig
        return base

    def _draw_poster(self, out: np.ndarray) -> None:
        src = self._poster_bgra
        if src is not None and src.size > 0 and not self._state.searching:
            if src.ndim == 3 and src.shape[2] == 3:
                src = cv2.cvtColor(src, cv2.COLOR_BGR2BGRA)
            # Cover-fit into poster rect.
            sh, sw = src.shape[:2]
            if sh >= 1 and sw >= 1:
                scale = max(_POSTER_W / float(sw), _POSTER_H / float(sh))
                nw = max(1, int(round(sw * scale)))
                nh = max(1, int(round(sh * scale)))
                resized = cv2.resize(
                    src,
                    (nw, nh),
                    interpolation=cv_resize_interp(sw, sh, nw, nh),
                )
                x0 = max(0, (nw - _POSTER_W) // 2)
                y0 = max(0, (nh - _POSTER_H) // 2)
                crop = resized[y0 : y0 + _POSTER_H, x0 : x0 + _POSTER_W]
                if crop.shape[0] != _POSTER_H or crop.shape[1] != _POSTER_W:
                    crop = cv2.resize(
                        crop, (_POSTER_W, _POSTER_H), interpolation=cv2.INTER_AREA
                    )
                mask = _rounded_rect_mask(_POSTER_W, _POSTER_H, _POSTER_RX)
                patch = crop.copy()
                if patch.shape[2] == 3:
                    patch = cv2.cvtColor(patch, cv2.COLOR_BGR2BGRA)
                patch[:, :, 3] = np.minimum(patch[:, :, 3], mask)
                _paste_patch_bgra(out, patch, _POSTER_X, _POSTER_Y)
        if self._state.searching:
            frames = self._ensure_search_frames()
            if frames:
                cx = int(round(_POSTER_X + _POSTER_W / 2.0))
                cy = int(round(_POSTER_Y + _POSTER_H / 2.0))
                patch = rotated_patch_for_angle(frames, self._state.search_angle_deg)
                blit_spinner_patch(out, patch, cx=cx, cy=cy)
        # Gray stroke is provided by SVG ``poster_accent`` chrome.

    def _draw_status_bar(self, out: np.ndarray) -> None:
        st = self._state
        pf = max(0.0, min(1.0, float(st.progress)))
        # Fixed remaining (full bar, grey) with full perimeter stroke.
        _draw_rounded_bar_bgra(
            out,
            x=_BAR_L,
            y=_BAR_T,
            w=_BAR_W,
            h=_BAR_H,
            fill_bgr=_COLOR_UNPLAYED_BGR,
            radius=_BAR_RX,
            stroke_bgr=_COLOR_CHROME_BGR,
            stroke=2,
        )
        # Elapsed grows from left; own full perimeter stroke (including leading edge).
        elapsed_w = max(_MIN_ELAPSED_W, int(round(pf * float(_BAR_W))))
        if pf <= 0.0:
            elapsed_w = _MIN_ELAPSED_W if st.elapsed_text else 0
        if elapsed_w > 0:
            _draw_rounded_bar_bgra(
                out,
                x=_BAR_L,
                y=_BAR_T,
                w=min(elapsed_w, _BAR_W),
                h=_BAR_H,
                fill_bgr=_COLOR_ACCENT_BGR,
                radius=_BAR_RX,
                stroke_bgr=_COLOR_CHROME_BGR,
                stroke=2,
            )
        # CTI: short vertical tick centered on the bar (does not reach elapsed text).
        cti_x = _BAR_L + min(elapsed_w, _BAR_W) - _CTI_W // 2
        cti_x = max(_BAR_L, min(_BAR_R - _CTI_W, cti_x))
        cti = np.zeros((_CTI_H, _CTI_W, 4), dtype=np.uint8)
        cti[:, :, :3] = _COLOR_CHROME_BGR
        cti[:, :, 3] = 255
        _paste_patch_bgra(out, cti, cti_x, _CTI_Y)

        et = str(st.elapsed_text or "").strip()
        rt = str(st.remaining_text or "").strip()
        rt_patch = None
        rt_w = rt_h = 0
        rt_x = _BAR_R
        if rt:
            rt_patch, rt_w, rt_h = _text_patch_digital7(rt, size_px=24)
            rt_x = _BAR_R - rt_w
            _paste_patch_bgra(
                out,
                rt_patch,
                rt_x,
                _REMAINING_TEXT_Y - rt_h // 2,
            )
        if et:
            et_patch, et_w, et_h = _text_patch_digital7(et, size_px=24)
            et_x = int(round(cti_x + _CTI_W / 2.0 - et_w / 2.0))
            # Drop elapsed label when it would collide with remaining.
            if et_x + et_w + _ELAPSED_REMAINING_GAP_PX < rt_x:
                _paste_patch_bgra(
                    out,
                    et_patch,
                    et_x,
                    _ELAPSED_TEXT_Y - et_h // 2,
                )

    def _draw_clock_group(self, out: np.ndarray) -> None:
        st = self._state
        now = datetime.now()
        date_p, _, _ = _text_patch_digital7(_date_mmddyy(now), size_px=36)
        _paste_centered(out, date_p, _DATE_CX, _DATE_CY)
        time_p, _, _ = _text_patch_digital7(_clock_hhmm_ampm(now), size_px=48)
        _paste_centered(out, time_p, _TIME_CX, _TIME_CY)
        rem = str(st.remaining_text or "").strip()
        if rem:
            # Prefer leading minus for remaining under circle (matches mock).
            if rem.upper() != "LIVE" and not rem.startswith("-"):
                rem_disp = f"-{rem}"
            else:
                rem_disp = rem
            rem_p, _, _ = _text_patch_digital7(rem_disp, size_px=48)
            _paste_centered(out, rem_p, _REMAINING_TIME_CX, _REMAINING_TIME_CY)

    def _draw_audio_group(self, out: np.ndarray) -> None:
        st = self._state
        vol = _receiver_volume_display_line(st.volume)
        if vol and vol.strip().lower() not in ("mute", "muted"):
            vol_p, _, _ = _text_patch_digital7(vol, size_px=72, max_width_px=180)
            _paste_centered(out, vol_p, _VOLUME_CX, _VOLUME_CY)
        cfg = receiver_audio_config_display_line(st.incoming, st.config)
        if cfg:
            cfg_p, _, _ = _text_patch_digital7(
                cfg.upper(),
                size_px=21,
                max_width_px=200,
            )
            _paste_centered(out, cfg_p, _AUDIO_CFG_CX, _AUDIO_CFG_CY)

    def _draw_cast(self, out: np.ndarray) -> None:
        cast = list(self._state.cast or [])
        while len(cast) < 3:
            cast.append(("", ""))
        for i, (center_x, actor_y, char_y) in enumerate(_CAST_COLS):
            actor, character = cast[i]
            if actor:
                ap, aw, _ah = _text_patch_digital7(
                    actor.upper(),
                    size_px=24,
                    max_width_px=_CAST_COL_W,
                )
                _paste_patch_bgra(out, ap, int(round(center_x - aw / 2.0)), actor_y)
            if character:
                cp, cw, _ch = _text_patch_digital7(
                    character.upper(),
                    size_px=18,
                    max_width_px=_CAST_COL_W,
                )
                _paste_patch_bgra(out, cp, int(round(center_x - cw / 2.0)), char_y)

    def _render_static_bgra(self) -> np.ndarray:
        out = _fallback_base_bgra()
        _paste_patch_bgra(out, self._render_svg_base(), 0, 0)
        self._draw_poster(out)
        # Identical pie-ring pipeline for playback (circle1) and volume (circle2).
        _draw_circle_pair(
            out,
            cx=_CIRCLE1_CX,
            cy=_CIRCLE1_CY,
            fraction=self._state.progress,
            show_accent=True,
        )
        vol_show = (not self._state.volume_muted) and self._state.volume_fraction > 1e-6
        _draw_circle_pair(
            out,
            cx=_CIRCLE2_CX,
            cy=_CIRCLE2_CY,
            fraction=self._state.volume_fraction,
            show_accent=vol_show,
        )
        self._draw_status_bar(out)
        self._draw_clock_group(out)
        self._draw_audio_group(out)
        self._draw_cast(out)
        return out

    def bgra_frame(self) -> np.ndarray | None:
        if not self._state.chrome_visible:
            return None
        sig = self._cache_sig()
        if self._cached_bgra is None or self._cached_sig != sig:
            self._cached_bgra = self._render_static_bgra()
            self._cached_sig = sig
        return self._cached_bgra

    def render(self, canvas_bgr: np.ndarray) -> None:
        if canvas_bgr is None or canvas_bgr.size == 0:
            return
        self.tick()
        if not self._state.chrome_visible:
            canvas_bgr[:] = (0, 0, 0)
            return
        frame = self.bgra_frame()
        if frame is None:
            return
        if frame.shape[2] >= 4:
            canvas_bgr[:] = alpha_blend_bgra_over_bgr(canvas_bgr, frame)
        else:
            h = min(canvas_bgr.shape[0], frame.shape[0])
            w = min(canvas_bgr.shape[1], frame.shape[1])
            canvas_bgr[:h, :w] = frame[:h, :w, :3]
