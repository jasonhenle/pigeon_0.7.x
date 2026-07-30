"""
Pigeon 0.8 ``view_circles`` now-playing skin (800×480).

Static chrome is rasterized from ``pigeonAssets/view_circles.svg`` (video) or
``pigeonAssets/view_circles_music.svg`` (music). Dynamic layers (cast / track
titles, clock, volume, progress rings, status bar, poster art) are drawn on top.
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

# Video poster (portrait TMDb).
_POSTER_VIDEO_X = 300
_POSTER_VIDEO_Y = 23
_POSTER_VIDEO_W = 200
_POSTER_VIDEO_H = 300
_POSTER_VIDEO_RX = 10

# Music poster: true 1:1 album-art frame (centered on design x=400).
_POSTER_MUSIC_X = 300
_POSTER_MUSIC_Y = 53
_POSTER_MUSIC_W = 200
_POSTER_MUSIC_H = 200
_POSTER_MUSIC_RX = 10

# Full-frame artwork backdrop under SVG chrome (music album art / video poster).
_ARTWORK_BG_OPACITY = 0.24  # 20% brighter than the original 0.20
_ARTWORK_BG_BLUR_DOWNSCALE = 4
_ARTWORK_BG_BLUR_SIGMA = 6.0

# Red accent fills (volume pie + progress pie + elapsed bar): normal alpha so blur shows through.
_ACCENT_OPACITY = 0.70
# Ellipse / rounded-rect outlines: keep a faint edge, mostly blur showing through.
_ACCENT_STROKE_PX = 2
_ACCENT_STROKE_OPACITY = 0.12
# Grey unplayed track / volume headroom: mostly see-through over artwork.
_CHROME_FILL_OPACITY = 0.12
# Dark inner discs (clock / volume centers): translucent so blur reads through.
_BUTTON_FILL_OPACITY = 0.35

# Back-compat aliases (video geometry).
_POSTER_X = _POSTER_VIDEO_X
_POSTER_Y = _POSTER_VIDEO_Y
_POSTER_W = _POSTER_VIDEO_W
_POSTER_H = _POSTER_VIDEO_H
_POSTER_RX = _POSTER_VIDEO_RX

# Music track titles — centered at cx=400 (SVG baselines ≈ song/album/artist y).
_TRACK_CX = 400.0
_TRACK_MAX_W = 360
_SONG_CY, _SONG_SIZE = 302.0, 36
_ALBUM_CY, _ALBUM_SIZE = 329.0, 24
_ARTIST_CY, _ARTIST_SIZE = 356.0, 24

_CONTENT_MODE_VIDEO = "video"
_CONTENT_MODE_MUSIC = "music"

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
_STRIP_SVG_IDS_COMMON: tuple[str, ...] = (
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

# Music demo track titles (redrawn programmatically). Keep ``poster_x5F_1X1_x5F_accent``.
_STRIP_SVG_IDS_MUSIC_EXTRA: tuple[str, ...] = (
    "song_x5F_title_x5F_text",
    "album_x5F_title_x5F_text",
    "artist_x5F_title",
)

_STRIP_SVG_IDS: tuple[str, ...] = _STRIP_SVG_IDS_COMMON
_STRIP_SVG_IDS_MUSIC: tuple[str, ...] = _STRIP_SVG_IDS_COMMON + _STRIP_SVG_IDS_MUSIC_EXTRA


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
    content_mode: str = _CONTENT_MODE_VIDEO  # "video" | "music"
    song_title: str = ""
    album_title: str = ""
    artist_title: str = ""
    # True while a TMDb / artwork fetch is in flight — poster shows the searching spinner.
    searching: bool = False
    search_angle_deg: float = 0.0


def _normalize_content_mode(mode: str | None) -> str:
    m = str(mode or "").strip().lower()
    if m == _CONTENT_MODE_MUSIC:
        return _CONTENT_MODE_MUSIC
    return _CONTENT_MODE_VIDEO


def default_view_circles_svg_path(
    assets_dir: Path | str | None = None,
    *,
    content_mode: str = _CONTENT_MODE_VIDEO,
) -> Path:
    mode = _normalize_content_mode(content_mode)
    if mode == _CONTENT_MODE_MUSIC:
        env = os.environ.get("PIGEON_VIEW_CIRCLES_MUSIC_SVG", "").strip()
        filename = "view_circles_music.svg"
    else:
        env = os.environ.get("PIGEON_VIEW_CIRCLES_SVG", "").strip()
        filename = "view_circles.svg"
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / filename
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / filename


def _poster_geometry(content_mode: str) -> tuple[int, int, int, int, int]:
    if _normalize_content_mode(content_mode) == _CONTENT_MODE_MUSIC:
        return (
            _POSTER_MUSIC_X,
            _POSTER_MUSIC_Y,
            _POSTER_MUSIC_W,
            _POSTER_MUSIC_H,
            _POSTER_MUSIC_RX,
        )
    return (
        _POSTER_VIDEO_X,
        _POSTER_VIDEO_Y,
        _POSTER_VIDEO_W,
        _POSTER_VIDEO_H,
        _POSTER_VIDEO_RX,
    )


def _cover_fit_bgra(src: np.ndarray, tw: int, th: int) -> np.ndarray:
    """Scale ``src`` to cover ``tw×th`` (centered crop). Returns BGRA."""
    if src is None or src.size == 0 or tw < 1 or th < 1:
        return np.zeros((max(1, th), max(1, tw), 4), dtype=np.uint8)
    arr = src
    if arr.ndim == 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGRA)
    elif arr.ndim == 3 and arr.shape[2] == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2BGRA)
    sh, sw = arr.shape[:2]
    if sh < 1 or sw < 1:
        return np.zeros((th, tw, 4), dtype=np.uint8)
    scale = max(tw / float(sw), th / float(sh))
    nw = max(1, int(round(sw * scale)))
    nh = max(1, int(round(sh * scale)))
    resized = cv2.resize(
        arr,
        (nw, nh),
        interpolation=cv_resize_interp(sw, sh, nw, nh),
    )
    x0 = max(0, (nw - tw) // 2)
    y0 = max(0, (nh - th) // 2)
    crop = resized[y0 : y0 + th, x0 : x0 + tw]
    if crop.shape[0] != th or crop.shape[1] != tw:
        crop = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_AREA)
    return crop


def _build_artwork_blur_bgra(src: np.ndarray) -> np.ndarray:
    """Full-frame cover-fit artwork, Gaussian-blurred, ~24% opacity (BGRA)."""
    tw, th = int(DESIGN_W), int(DESIGN_H)
    cover = _cover_fit_bgra(src, tw, th)
    dw = max(1, tw // _ARTWORK_BG_BLUR_DOWNSCALE)
    dh = max(1, th // _ARTWORK_BG_BLUR_DOWNSCALE)
    small = cv2.resize(cover, (dw, dh), interpolation=cv2.INTER_AREA)
    bgr = small[:, :, :3]
    sigma = float(_ARTWORK_BG_BLUR_SIGMA)
    k = max(3, int(round(sigma * 2)) | 1)
    blurred = cv2.GaussianBlur(bgr, (k, k), sigmaX=sigma, sigmaY=sigma)
    up = cv2.resize(blurred, (tw, th), interpolation=cv2.INTER_LINEAR)
    out = np.zeros((th, tw, 4), dtype=np.uint8)
    out[:, :, :3] = up
    out[:, :, 3] = int(round(255.0 * _ARTWORK_BG_OPACITY))
    return out


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
            matrix=fitz.Matrix(src_w / page.rect.width, src_h / page.rect.height),
            alpha=True,
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


def _decanvas_white_bgra(src: np.ndarray, *, threshold: int = 252) -> np.ndarray:
    """Make PyMuPDF/cairosvg white canvas pixels transparent before compositing."""
    if src is None or src.size == 0 or src.ndim != 3 or src.shape[2] < 4:
        return src
    out = src.copy()
    rgb = out[:, :, :3]
    white = (
        (rgb[:, :, 0] >= threshold)
        & (rgb[:, :, 1] >= threshold)
        & (rgb[:, :, 2] >= threshold)
    )
    out[white, 3] = 0
    return out


def apply_view_circles_svg_state(
    root: ET.Element,
    *,
    content_mode: str = _CONTENT_MODE_VIDEO,
) -> None:
    mode = _normalize_content_mode(content_mode)
    # Transparent page so the artwork blur layer can sit under chrome.
    root.set("style", "background:transparent")
    _remove_element_by_id(root, "background")
    strip_ids = (
        _STRIP_SVG_IDS_MUSIC if mode == _CONTENT_MODE_MUSIC else _STRIP_SVG_IDS
    )
    for element_id in strip_ids:
        _remove_element_by_id(root, element_id)


def render_view_circles_svg_base_bgra(
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
    content_mode: str = _CONTENT_MODE_VIDEO,
) -> np.ndarray:
    mode = _normalize_content_mode(content_mode)
    if svg_path is not None:
        path = Path(svg_path)
    else:
        path = default_view_circles_svg_path(assets_dir, content_mode=mode)
    if not path.is_file():
        raise FileNotFoundError(f"view_circles SVG not found: {path}")
    root = _svg_tree_from_path(path)
    apply_view_circles_svg_state(root, content_mode=mode)
    bgra = _rasterize_svg_tree(root)
    # Rasterizers often paint a white page behind transparent SVG roots.
    bgra = _decanvas_white_bgra(bgra)
    return bgra


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


def _restore_masked_region(
    canvas: np.ndarray,
    under: np.ndarray,
    mask: np.ndarray,
    *,
    x: int,
    y: int,
) -> None:
    """Copy ``under`` into ``canvas`` where ``mask`` > 0 (same H×W as ``under``/``mask``)."""
    if (
        canvas is None
        or canvas.size == 0
        or under is None
        or under.size == 0
        or mask is None
        or mask.size == 0
    ):
        return
    mh, mw = mask.shape[:2]
    if under.shape[0] != mh or under.shape[1] != mw:
        return
    x0 = int(x)
    y0 = int(y)
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1 = min(int(canvas.shape[1]), x0 + mw)
    cy1 = min(int(canvas.shape[0]), y0 + mh)
    if cx0 >= cx1 or cy0 >= cy1:
        return
    sx0, sy0 = cx0 - x0, cy0 - y0
    m = mask[sy0 : sy0 + (cy1 - cy0), sx0 : sx0 + (cx1 - cx0)] > 0
    if not np.any(m):
        return
    src = under[sy0 : sy0 + (cy1 - cy0), sx0 : sx0 + (cx1 - cx0)]
    roi = canvas[cy0:cy1, cx0:cx1]
    roi[m] = src[m]


def _restore_from_backdrop_abs(
    canvas: np.ndarray,
    mask: np.ndarray,
    *,
    x: int,
    y: int,
    backdrop: np.ndarray,
    backdrop_x: int,
    backdrop_y: int,
) -> None:
    """Where ``mask`` > 0 at canvas ``(x,y)``, copy from ``backdrop`` using absolute coords."""
    if (
        canvas is None
        or canvas.size == 0
        or backdrop is None
        or backdrop.size == 0
        or mask is None
        or mask.size == 0
    ):
        return
    mh, mw = mask.shape[:2]
    bh, bw = int(backdrop.shape[0]), int(backdrop.shape[1])
    x0, y0 = int(x), int(y)
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1 = min(int(canvas.shape[1]), x0 + mw)
    cy1 = min(int(canvas.shape[0]), y0 + mh)
    if cx0 >= cx1 or cy0 >= cy1:
        return
    sx0, sy0 = cx0 - x0, cy0 - y0
    roi_h, roi_w = cy1 - cy0, cx1 - cx0
    m = mask[sy0 : sy0 + roi_h, sx0 : sx0 + roi_w] > 0
    if not np.any(m):
        return
    yy, xx = np.nonzero(m)
    by = (cy0 - int(backdrop_y)) + yy
    bx = (cx0 - int(backdrop_x)) + xx
    valid = (by >= 0) & (by < bh) & (bx >= 0) & (bx < bw)
    if not np.any(valid):
        return
    yy, xx, by, bx = yy[valid], xx[valid], by[valid], bx[valid]
    canvas[cy0 + yy, cx0 + xx] = backdrop[by, bx]


def _paste_stroke_over_blur(
    canvas: np.ndarray,
    stroke_patch: np.ndarray,
    x: int,
    y: int,
    *,
    backdrop: np.ndarray | None,
    backdrop_x: int,
    backdrop_y: int,
    opacity: float,
) -> None:
    """Restore blurred backdrop under stroke coverage, then blend stroke at ``opacity``."""
    if stroke_patch is None or stroke_patch.size == 0:
        return
    coverage = stroke_patch[:, :, 3]
    if not np.any(coverage):
        return
    if backdrop is not None:
        _restore_from_backdrop_abs(
            canvas,
            coverage,
            x=x,
            y=y,
            backdrop=backdrop,
            backdrop_x=backdrop_x,
            backdrop_y=backdrop_y,
        )
    sop = max(0.0, min(1.0, float(opacity)))
    if sop >= 1.0 - 1e-6:
        _paste_patch_bgra(canvas, stroke_patch, x, y)
        return
    patch = stroke_patch.copy()
    patch[:, :, 3] = np.clip(patch[:, :, 3].astype(np.float32) * sop, 0, 255).astype(
        np.uint8
    )
    _paste_patch_bgra(canvas, patch, x, y)


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
    fill_opacity: float = 1.0,
    stroke_opacity: float = 1.0,
    stroke_backdrop: np.ndarray | None = None,
    stroke_backdrop_x: int = 0,
    stroke_backdrop_y: int = 0,
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
    fill_a = int(round(255.0 * max(0.0, min(1.0, float(fill_opacity)))))
    fill = np.zeros((mh, mw, 4), dtype=np.uint8)
    fill[mask > 0, :3] = fill_bgr
    fill[mask > 0, 3] = fill_a
    _paste_patch_bgra(bgra, fill, x0 - pad, y0 - pad)
    if stroke_bgr is not None and stroke > 0:
        # Full perimeter stroke (not just the mask edge after erode — that missed ends).
        stroke_patch = np.zeros((mh, mw, 4), dtype=np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cv2.drawContours(
                stroke_patch,
                contours,
                -1,
                (*stroke_bgr, 255),
                thickness=max(1, int(stroke)),
                lineType=cv2.LINE_AA,
            )
            _paste_stroke_over_blur(
                bgra,
                stroke_patch,
                x0 - pad,
                y0 - pad,
                backdrop=stroke_backdrop,
                backdrop_x=stroke_backdrop_x,
                backdrop_y=stroke_backdrop_y,
                opacity=stroke_opacity,
            )


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
    fill_opacity: float = 1.0,
) -> None:
    radius = max(1, int(round(r)))
    pad = stroke + 2
    size = radius * 2 + pad * 2
    center = (size // 2, size // 2)
    # Mask-driven alpha (more reliable than cv2 BGRA Scalar alpha + LINE_AA).
    mask = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(mask, center, radius, 255, -1, lineType=cv2.LINE_AA)
    op = max(0.0, min(1.0, float(fill_opacity)))
    patch = np.zeros((size, size, 4), dtype=np.uint8)
    patch[:, :, 0] = fill_bgr[0]
    patch[:, :, 1] = fill_bgr[1]
    patch[:, :, 2] = fill_bgr[2]
    patch[:, :, 3] = np.clip(mask.astype(np.float32) * op, 0, 255).astype(np.uint8)
    x = int(round(cx - size / 2.0))
    y = int(round(cy - size / 2.0))
    _paste_patch_bgra(bgra, patch, x, y)
    if stroke > 0:
        # Stroke stays fully opaque so the ring edge stays crisp over translucent fill.
        stroke_patch = np.zeros((size, size, 4), dtype=np.uint8)
        cv2.circle(
            stroke_patch, center, radius, (*stroke_bgr, 255), stroke, lineType=cv2.LINE_AA
        )
        _paste_patch_bgra(bgra, stroke_patch, x, y)


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
    stroke: int = _ACCENT_STROKE_PX,
    fill_opacity: float = _ACCENT_OPACITY,
    stroke_opacity: float = _ACCENT_STROKE_OPACITY,
    stroke_backdrop: np.ndarray | None = None,
    stroke_backdrop_x: int = 0,
    stroke_backdrop_y: int = 0,
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
    fill_a = int(round(255.0 * max(0.0, min(1.0, float(fill_opacity)))))
    fill = np.zeros((size, size, 4), dtype=np.uint8)
    fill[mask > 0, :3] = fill_bgr
    fill[mask > 0, 3] = fill_a
    x = int(round(cx - size / 2.0))
    y = int(round(cy - size / 2.0))
    _paste_patch_bgra(bgra, fill, x, y)
    if stroke > 0:
        # Arc strokes (not full contour) — contour stroke left a gap at 12-o'clock on small pies.
        stroke_patch = np.zeros((size, size, 4), dtype=np.uint8)
        cx_i = cy_i = size // 2
        outer = max(1, int(round(outer_r)))
        inner = max(0, min(int(round(inner_r)), outer - 1))
        start = -90.0
        end = start + 360.0 * frac
        thick = max(1, int(stroke))
        color = (*stroke_bgr, 255)
        if frac >= 0.999:
            cv2.circle(stroke_patch, (cx_i, cy_i), outer, color, thick, lineType=cv2.LINE_AA)
            if inner > 0:
                cv2.circle(stroke_patch, (cx_i, cy_i), inner, color, thick, lineType=cv2.LINE_AA)
        else:
            cv2.ellipse(
                stroke_patch,
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
                    stroke_patch,
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
                cv2.line(
                    stroke_patch, (x_i, y_i), (x_o, y_o), color, thick, lineType=cv2.LINE_AA
                )
        _paste_stroke_over_blur(
            bgra,
            stroke_patch,
            x,
            y,
            backdrop=stroke_backdrop,
            backdrop_x=stroke_backdrop_x,
            backdrop_y=stroke_backdrop_y,
            opacity=stroke_opacity,
        )


def _draw_circle_pair(
    bgra: np.ndarray,
    *,
    cx: float,
    cy: float,
    fraction: float,
    show_accent: bool,
) -> None:
    """Headroom disc + optional pie accent + inner button (identical for circle1 / circle2)."""
    frac = max(0.0, min(1.0, float(fraction)))
    # Snapshot underlayer so the red pie can sit over artwork, not solid grey.
    under = None
    ring_x = ring_y = ring_size = 0
    if show_accent and frac > 1e-6:
        pad = 6
        ring_size = int(math.ceil(_RING_OUTER_R * 2)) + pad * 2
        ring_x = int(round(cx - ring_size / 2.0))
        ring_y = int(round(cy - ring_size / 2.0))
        x0, y0 = max(0, ring_x), max(0, ring_y)
        x1 = min(int(DESIGN_W), ring_x + ring_size)
        y1 = min(int(DESIGN_H), ring_y + ring_size)
        if x0 < x1 and y0 < y1:
            under = bgra[y0:y1, x0:x1].copy()
            # Pad under to full ring_size so mask coords match.
            if under.shape[0] != ring_size or under.shape[1] != ring_size:
                full = np.zeros((ring_size, ring_size, bgra.shape[2]), dtype=bgra.dtype)
                full[y0 - ring_y : y1 - ring_y, x0 - ring_x : x1 - ring_x] = under
                under = full

    _draw_filled_circle_bgra(
        bgra,
        cx=cx,
        cy=cy,
        r=_RING_OUTER_R,
        fill_bgr=_COLOR_UNPLAYED_BGR,
        fill_opacity=_CHROME_FILL_OPACITY,
    )
    if show_accent and frac > 1e-6:
        if under is not None:
            mask = annular_sector_mask(
                ring_size,
                outer_r=_RING_OUTER_R,
                inner_r=_RING_INNER_R,
                fraction=frac,
            )
            _restore_masked_region(bgra, under, mask, x=ring_x, y=ring_y)
        _draw_progress_ring(
            bgra,
            cx=cx,
            cy=cy,
            outer_r=_RING_OUTER_R,
            inner_r=_RING_INNER_R,
            fraction=frac,
            fill_opacity=_ACCENT_OPACITY,
            stroke_opacity=_ACCENT_STROKE_OPACITY,
            stroke_backdrop=under,
            stroke_backdrop_x=ring_x,
            stroke_backdrop_y=ring_y,
        )
    _draw_filled_circle_bgra(
        bgra,
        cx=cx,
        cy=cy,
        r=_RING_INNER_R,
        fill_bgr=_COLOR_BUTTON_BGR,
        fill_opacity=_BUTTON_FILL_OPACITY,
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
        # Dual chrome caches keyed by content_mode ("video" | "music").
        self._svg_chrome_by_mode: dict[str, np.ndarray] = {}
        self._svg_chrome_sig_by_mode: dict[str, tuple[object, ...]] = {}
        # Cached artwork blur backdrop (keyed by id of ``_poster_bgra``).
        self._artwork_blur_bgra: np.ndarray | None = None
        self._artwork_blur_poster_id: int | None = None
        self._search_frames: tuple[np.ndarray, ...] | None = None
        self._search_frames_tried = False
        self._last_tick_mono: float | None = None

    @property
    def chrome_visible(self) -> bool:
        return self._state.chrome_visible

    @property
    def searching(self) -> bool:
        return bool(self._state.searching)

    @property
    def content_mode(self) -> str:
        return _normalize_content_mode(self._state.content_mode)

    def clear_cache(self) -> None:
        self._cached_bgra = None
        self._cached_sig = None

    def _clear_artwork_blur_cache(self) -> None:
        self._artwork_blur_bgra = None
        self._artwork_blur_poster_id = None

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
            self._clear_artwork_blur_cache()
            self.clear_cache()
            return True
        arr = np.asarray(poster_bgra, dtype=np.uint8)
        if self._poster_bgra is not None and self._poster_bgra.shape == arr.shape:
            if np.array_equal(self._poster_bgra, arr):
                return False
        self._poster_bgra = arr.copy()
        self._clear_artwork_blur_cache()
        self.clear_cache()
        return True

    def _ensure_artwork_blur_bgra(self) -> np.ndarray | None:
        src = self._poster_bgra
        if src is None or src.size == 0:
            self._clear_artwork_blur_cache()
            return None
        pid = id(src)
        if self._artwork_blur_bgra is not None and self._artwork_blur_poster_id == pid:
            return self._artwork_blur_bgra
        self._artwork_blur_bgra = _build_artwork_blur_bgra(src)
        self._artwork_blur_poster_id = pid
        return self._artwork_blur_bgra

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
        content_mode: str | None = None,
        song_title: str | None = None,
        album_title: str | None = None,
        artist_title: str | None = None,
    ) -> bool:
        changed = False
        if self.set_now_playing_chrome_visible(has_now_playing):
            changed = True
        if content_mode is not None:
            mode = _normalize_content_mode(content_mode)
            if mode != self._state.content_mode:
                self._state.content_mode = mode
                changed = True
        is_music = self._state.content_mode == _CONTENT_MODE_MUSIC
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
        if is_music:
            if song_title is not None:
                st = str(song_title or "")
                if st != self._state.song_title:
                    self._state.song_title = st
                    changed = True
            if album_title is not None:
                al = str(album_title or "")
                if al != self._state.album_title:
                    self._state.album_title = al
                    changed = True
            if artist_title is not None:
                ar = str(artist_title or "")
                if ar != self._state.artist_title:
                    self._state.artist_title = ar
                    changed = True
            # Music layout has no cast row.
            if self._state.cast:
                self._state.cast = []
                changed = True
        else:
            if self._state.song_title or self._state.album_title or self._state.artist_title:
                self._state.song_title = ""
                self._state.album_title = ""
                self._state.artist_title = ""
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
            14,  # cache schema version (accent stroke over blur only)
            st.content_mode,
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
            st.song_title,
            st.album_title,
            st.artist_title,
            poster_id,
            st.searching,
            search_frame,
            int(now.strftime("%H%M")),
            f"{now.month}/{now.day}/{now.strftime('%y')}",
        )

    def _svg_chrome_cache_sig(self, content_mode: str) -> tuple[object, ...]:
        mode = _normalize_content_mode(content_mode)
        path = default_view_circles_svg_path(self._assets_dir, content_mode=mode)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = -1
        # Bump when strip/redraw pipeline changes so cached chrome is not reused.
        return (str(path), mtime, mode, 7)

    def _render_svg_base(self) -> np.ndarray:
        mode = self.content_mode
        sig = self._svg_chrome_cache_sig(mode)
        cached = self._svg_chrome_by_mode.get(mode)
        if cached is not None and self._svg_chrome_sig_by_mode.get(mode) == sig:
            return cached
        try:
            base = render_view_circles_svg_base_bgra(
                assets_dir=self._assets_dir,
                content_mode=mode,
            )
        except Exception:
            base = _fallback_base_bgra()
        self._svg_chrome_by_mode[mode] = base
        self._svg_chrome_sig_by_mode[mode] = sig
        return base

    def _draw_poster(self, out: np.ndarray) -> None:
        px, py, pw, ph, prx = _poster_geometry(self.content_mode)
        src = self._poster_bgra
        if src is not None and src.size > 0 and not self._state.searching:
            if src.ndim == 3 and src.shape[2] == 3:
                src = cv2.cvtColor(src, cv2.COLOR_BGR2BGRA)
            # Cover-fit into poster rect.
            sh, sw = src.shape[:2]
            if sh >= 1 and sw >= 1:
                scale = max(pw / float(sw), ph / float(sh))
                nw = max(1, int(round(sw * scale)))
                nh = max(1, int(round(sh * scale)))
                resized = cv2.resize(
                    src,
                    (nw, nh),
                    interpolation=cv_resize_interp(sw, sh, nw, nh),
                )
                x0 = max(0, (nw - pw) // 2)
                y0 = max(0, (nh - ph) // 2)
                crop = resized[y0 : y0 + ph, x0 : x0 + pw]
                if crop.shape[0] != ph or crop.shape[1] != pw:
                    crop = cv2.resize(crop, (pw, ph), interpolation=cv2.INTER_AREA)
                mask = _rounded_rect_mask(pw, ph, prx)
                patch = crop.copy()
                if patch.shape[2] == 3:
                    patch = cv2.cvtColor(patch, cv2.COLOR_BGR2BGRA)
                patch[:, :, 3] = np.minimum(patch[:, :, 3], mask)
                _paste_patch_bgra(out, patch, px, py)
        if self._state.searching:
            frames = self._ensure_search_frames()
            if frames:
                cx = int(round(px + pw / 2.0))
                cy = int(round(py + ph / 2.0))
                patch = rotated_patch_for_angle(frames, self._state.search_angle_deg)
                blit_spinner_patch(out, patch, cx=cx, cy=cy)
        # Gray stroke is provided by SVG poster accent chrome.

    def _draw_status_bar(self, out: np.ndarray) -> None:
        st = self._state
        pf = max(0.0, min(1.0, float(st.progress)))
        # Elapsed grows from left; own full perimeter stroke (including leading edge).
        elapsed_w = max(_MIN_ELAPSED_W, int(round(pf * float(_BAR_W))))
        if pf <= 0.0:
            elapsed_w = _MIN_ELAPSED_W if st.elapsed_text else 0
        elapsed_w = min(elapsed_w, _BAR_W) if elapsed_w > 0 else 0
        # Snapshot underlayer so translucent red sits over artwork, not solid grey.
        under = None
        bar_pad = 3  # matches stroke pad in _draw_rounded_bar_bgra (stroke=2)
        bar_x = _BAR_L - bar_pad
        bar_y = _BAR_T - bar_pad
        if elapsed_w > 0:
            ew = elapsed_w + bar_pad * 2
            eh = _BAR_H + bar_pad * 2
            x0, y0 = max(0, bar_x), max(0, bar_y)
            x1 = min(int(DESIGN_W), bar_x + ew)
            y1 = min(int(DESIGN_H), bar_y + eh)
            if x0 < x1 and y0 < y1:
                under = np.zeros((eh, ew, out.shape[2]), dtype=out.dtype)
                under[y0 - bar_y : y1 - bar_y, x0 - bar_x : x1 - bar_x] = out[y0:y1, x0:x1]
        # Fixed remaining (full bar, grey) with faint perimeter stroke over blur.
        _draw_rounded_bar_bgra(
            out,
            x=_BAR_L,
            y=_BAR_T,
            w=_BAR_W,
            h=_BAR_H,
            fill_bgr=_COLOR_UNPLAYED_BGR,
            radius=_BAR_RX,
            stroke_bgr=_COLOR_CHROME_BGR,
            stroke=_ACCENT_STROKE_PX,
            fill_opacity=_CHROME_FILL_OPACITY,
            stroke_opacity=_ACCENT_STROKE_OPACITY,
        )
        if elapsed_w > 0:
            if under is not None:
                lw, lh = elapsed_w, _BAR_H
                mw, mh = lw + bar_pad * 2, lh + bar_pad * 2
                mask = np.zeros((mh, mw), dtype=np.uint8)
                inner = _rounded_rect_mask(lw, lh, min(_BAR_RX, lw // 2, lh // 2))
                mask[bar_pad : bar_pad + lh, bar_pad : bar_pad + lw] = inner
                _restore_masked_region(out, under, mask, x=bar_x, y=bar_y)
            _draw_rounded_bar_bgra(
                out,
                x=_BAR_L,
                y=_BAR_T,
                w=elapsed_w,
                h=_BAR_H,
                fill_bgr=_COLOR_ACCENT_BGR,
                radius=_BAR_RX,
                stroke_bgr=_COLOR_CHROME_BGR,
                stroke=_ACCENT_STROKE_PX,
                fill_opacity=_ACCENT_OPACITY,
                stroke_opacity=_ACCENT_STROKE_OPACITY,
                stroke_backdrop=under,
                stroke_backdrop_x=bar_x,
                stroke_backdrop_y=bar_y,
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
        # Music layout has no remaining-time label under circle1.
        if st.content_mode == _CONTENT_MODE_MUSIC:
            return
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

    def _draw_track_titles(self, out: np.ndarray) -> None:
        st = self._state
        for text, cy, size in (
            (st.song_title, _SONG_CY, _SONG_SIZE),
            (st.album_title, _ALBUM_CY, _ALBUM_SIZE),
            (st.artist_title, _ARTIST_CY, _ARTIST_SIZE),
        ):
            label = str(text or "").strip()
            if not label:
                continue
            patch, _, _ = _text_patch_digital7(
                label.upper(),
                size_px=size,
                max_width_px=_TRACK_MAX_W,
            )
            _paste_centered(out, patch, _TRACK_CX, cy)

    def _render_static_bgra(self) -> np.ndarray:
        out = _fallback_base_bgra()
        # Blurred cover-fit artwork under chrome (music album art / video poster).
        if (
            self._poster_bgra is not None
            and self._poster_bgra.size > 0
            and not self._state.searching
        ):
            blur = self._ensure_artwork_blur_bgra()
            if blur is not None:
                _paste_patch_bgra(out, blur, 0, 0)
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
        if self.content_mode == _CONTENT_MODE_MUSIC:
            self._draw_track_titles(out)
        else:
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
