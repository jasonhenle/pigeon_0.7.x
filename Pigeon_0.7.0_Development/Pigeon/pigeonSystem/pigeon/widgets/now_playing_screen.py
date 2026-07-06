"""
Pigeon 0.7 now-playing screen — SVG chrome from ``now_playing_test_070326`` (800×480).

Static chrome is rasterized from ``pigeonAssets/now_playing_test_070326.svg``. Dynamic
layers (played/unplayed bar groups, TMDb TT + backdrop, badge, timecode, audio meters,
clock, and programmatic overlays) are drawn programmatically on top.
"""

from __future__ import annotations

import io
import math
import os
import random
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

from pigeon.compositing import (
    alpha_blend_bgra_over_bgr,
    bgr_to_red_monochrome_luma,
    cv_resize_interp,
)
from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.font_paths import resolve_ui_font_bold, resolve_ui_font_extrabold, resolve_ui_font_medium
from pigeon.image_ui_protocol import load_image_bgra
from pigeon.widgets.playback_overlay import (
    _image_contain_center_bgra,
    _looks_like_receiver_debug_blob,
    _receiver_audio_display_line,
    _receiver_volume_display_line,
    _text_patch_bgra,
    receiver_audio_config_display_line,
)
from pigeon.widgets.status_bar import DesignPatch

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# --- Colors (Numbers spec) ---
_COLOR_UI_HEX = "#D3001A"
_COLOR_ACCENT_HEX = "#FFFFFF"
_COLOR_BG_HEX = "#000000"
_COLOR_SUCCESS_HEX = "#01D800"
_COLOR_FAIL_HEX = "#E10018"
_COLOR_UNPLAYED_HEX = "#282828"

_COLOR_UI_BGR = (26, 0, 211)  # #D3001A
_COLOR_PLAYED_BGR = _COLOR_UI_BGR
_COLOR_ACCENT_BGR = (255, 255, 255)
_COLOR_BG_BGR = (0, 0, 0)
_COLOR_SUCCESS_BGR = (0, 216, 1)
_COLOR_FAIL_BGR = (24, 0, 225)
_COLOR_UNPLAYED_BGR = (40, 40, 40)

_SVG_W = 800.0
_SVG_H = 480.0
_Y_SCALE = float(DESIGN_H) / _SVG_H

# Logical Illustrator layer ids (encoded in SVG via _encode_svg_layer_id).
_HIDE_LAYER_LOGICAL: tuple[str, ...] = (
    "04_widget_now_playing_status_bar_played",
    "04_widget_now_playing_status_bar_unplayed",
    "03_widget_now_playing_status_bar_played",
    "03_widget_now_playing_status_bar_unplayed",
    "04_widget_now_playing_tmdb_TT_normal",
    "03_widget_now_playing_tmdb_TT_normal",
    "03_widget_now_playing_tmdb_TT_black",
    "03_widget_now_playing_tmdb_TT_unplayed",
    "03_widget_now_playing_tmdb_TT_played",
    "04_widget_backdrop_tmdb_backdrop",
    "04_widget_backdrop_tmdb_color",
    "04_now_playing_color_two",
    "03_widget_now_playing_stroke",
    "03_widget_now_playing_timecode_stroke",
    "03_widget_now_playing_timecode_container",
    "03_widget_now_playing_timecode_text",
    "03_widget_now_playing_content_time",
    "05_now_playing_service_text",
    "02_widget_clock_text",
    "05_widget_audio_config_text",
    "05_widget_audio_config_volume_text",
    "06_widget_audio_levels_lfe_scale",
    "06_widget_audio_levels_sl_scale",
    "06_widget_audio_levels_l_scale",
    "06_widget_audio_levels_c_scale",
    "06_widget_audio_levels_r_scale",
    "06_widget_audio_levels_sr_scale",
    "06_widget_audio_levels_sl_text",
    "06_widget_audio_levels_l_text",
    "06_widget_audio_levels_c_text",
    "06_widget_audio_levels_r_text",
    "06_widget_audio_levels_sr_text",
    "06_widget_audio_levels_lfe_text",
    "06_widget_audio_levels_LFE_text",
)

_INDICATOR_LAYER_LOGICAL: tuple[tuple[str, str], ...] = (
    ("01_icon_audio_indicator", "indicator_audio"),
    ("01_icon_now_playing_indicator", "indicator_now_playing"),
    ("01_icon_indicator_reveiver", "indicator_receiver"),
    ("01_icon_indicator_tmdb", "indicator_tmdb"),
)

# Illustrator ``id`` attrs as authored in ``now_playing_test_070326.svg`` (leading ``_0``).
_DIRECT_STRIP_SVG_IDS: tuple[str, ...] = (
    "_05_widget_audio_config_text",
    "_05_widget_audio_config_volume_text",
)


def _encode_svg_layer_id(logical_id: str) -> str:
    """Map ``07_background`` → ``_x30_7_x5F_background`` (Illustrator XML id encoding)."""
    body = logical_id
    if body.startswith("0"):
        body = "_x30_" + body[1:]
    return body.replace("_", "_x5F_")


def _sy(y_svg: float) -> int:
    return int(round(y_svg * _Y_SCALE))


def _sx(x_svg: float) -> int:
    return int(round(x_svg))


# Now-playing bar (layer 03) — from ``now_playing_070326`` bounds.
_BAR_L = 36
_BAR_T = 39
_BAR_W = 731
_BAR_H = 219
_BAR_RX = 28
_CONTENT_PAD = 50
_IMAGE_CORNER_RX = 12
_TT_TINT_WHITE = 0.50
_PLAYED_STROKE_PX = 3

# Timecode container (tracks played edge).
_CONTAINER_W = 164
_TC_W = _CONTAINER_W
_TC_H = 50
_TC_Y = 247

# Clock + service + audio chrome (layers 02/05).
# Clock right edge aligns to the rightmost status dot (``01_icon_indicator_tmdb`` in SVG).
_STATUS_INDICATOR_TMDB_CX = 753
_STATUS_INDICATOR_DOT_R = 8
_CLOCK_RIGHT_X = _STATUS_INDICATOR_TMDB_CX + _STATUS_INDICATOR_DOT_R
# Shared baselines (SVG ``02_widget_clock_text`` y≈405; config/service row y≈453).
_UPPER_BASELINE_Y = 405
_LOWER_BASELINE_Y = 453
_CLOCK_SIZE_PX = 60
_VOLUME_SIZE_PX = _CLOCK_SIZE_PX
_SERVICE_TEXT_SIZE_PX = 30
# Right edge of service label (right-aligned); 120px inset keeps it clear of the status dots.
_SERVICE_TEXT_RIGHT_X = int(DESIGN_W) - 120
_AUDIO_CFG_TEXT_SIZE = 25
_AUDIO_CFG_MAX_W = 280
# Right edge of the LFE meter column — volume is centered between here and the clock.
_AUDIO_METER_RIGHT_X = 222
# Drop shadow on TMDb TT in the played (watched) bar group only.
_TT_PLAYED_DROP_SHADOW = True
_TT_SHADOW_OFFSET = (5, 6)
_TT_SHADOW_BLUR_SIGMA = 5.0
_TT_SHADOW_STRENGTH = 0.88
# SVG circle centers for the four status dots (layer 01).
_STATUS_INDICATOR_CENTERS: tuple[tuple[float, float], ...] = (
    (692.89, 444.33),  # audio
    (712.89, 444.33),  # now playing
    (732.89, 444.33),  # receiver
    (752.89, 444.33),  # tmdb
)
# Targeted bottom wipes — must not cover status dots (x≈685+), clock (y≈340–415, x≈520+).
_BOTTOM_CONFIG_WIPE_TOP = 446
_BOTTOM_MID_WIPE_TOP = 412
_BOTTOM_TEXT_WIPE_RIGHT_X = 678

# --- Variation layouts (np = status bar, rv = receiver info, ck = clock) ---
# "full" keeps the 070326 SVG layout with audio meters (HDMI analysis / sim).
# The other modes drop the meters and re-anchor text per the 070426 mocks.
_LAYOUT_MODES = ("full", "np_rv_ck", "np_ck", "rv_ck", "ck")
# np_* modes: text row under the bar (volume/clock upper, config/service lower).
_NP_UPPER_BASELINE_Y = 368
_NP_LOWER_BASELINE_Y = 426
_NP_LEFT_X = 58
_NP_CLOCK_SIZE_PX = 72
_NP_VOLUME_SIZE_PX = 72
_NP_TEXT_SIZE_PX = 30
_NP_SERVICE_RIGHT_X = _BOTTOM_TEXT_WIPE_RIGHT_X  # service label ends left of the dots
# rv_ck / ck modes (no now-playing bar): right-aligned stack with a large clock.
_RV_RIGHT_X = _CLOCK_RIGHT_X
_RV_VOLUME_BASELINE_Y = 122
_RV_VOLUME_SIZE_PX = 64
_RV_CFG_BASELINE_Y = 192
_RV_CFG_SIZE_PX = 40
_RV_CLOCK_BASELINE_Y = 362
_RV_CLOCK_SIZE_PX = 128


def _normalized_layout_mode(mode: str) -> str:
    m = str(mode or "").strip().lower()
    return m if m in _LAYOUT_MODES else "np_rv_ck"

# Audio level meters (layer 06) — x, baseline_y, bar_w, max_height_px (bottom = upper baseline).
_AUDIO_CHANNELS: tuple[tuple[str, int, int, int, int], ...] = (
    ("SL", 53, _UPPER_BASELINE_Y, 11, 23),
    ("L", 76, _UPPER_BASELINE_Y, 11, 44),
    ("C", 99, _UPPER_BASELINE_Y, 11, 87),
    ("R", 124, _UPPER_BASELINE_Y, 11, 44),
    ("SR", 150, _UPPER_BASELINE_Y, 11, 23),
    ("LFE", 211, _UPPER_BASELINE_Y, 11, 9),
)
# Channel abbreviations share the lower baseline with service + audio config.
_AUDIO_LABELS: tuple[tuple[str, int, int], ...] = (
    ("SL", 46, _LOWER_BASELINE_Y),
    ("L", 76, _LOWER_BASELINE_Y),
    ("C", 99, _LOWER_BASELINE_Y),
    ("R", 124, _LOWER_BASELINE_Y),
    ("SR", 142, _LOWER_BASELINE_Y),
    ("LFE", 200, _LOWER_BASELINE_Y),
)
_CONTAINER_RX = 12

@dataclass
class NowPlayingScreenState:
    """External inputs mirrored from pigeon_0_7 holders."""

    progress: float = 0.0
    remaining_text: str = ""
    show_paused: bool = False
    chrome_visible: bool = False
    trt_substantive: bool = False
    theater_dim_suppressed: bool = False
    incoming: str = ""
    config: str = ""
    volume: str = ""
    badge_show: bool = False
    badge_filename: str = ""
    badge_label: str = ""
    indicator_now_playing: bool = False
    indicator_receiver: bool = False
    indicator_tmdb: bool = False
    indicator_audio: bool = False
    audio_levels_sim: bool = False
    # "full" | "np_rv_ck" | "np_ck" | "rv_ck" | "ck" — see _LAYOUT_MODES.
    layout_mode: str = "np_rv_ck"


def default_now_playing_svg_path(assets_dir: Path | str | None = None) -> Path:
    """Resolve now-playing SVG (override with ``PIGEON_NOW_PLAYING_SVG``)."""
    env = os.environ.get("PIGEON_NOW_PLAYING_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "now_playing_test_070326.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "now_playing_test_070326.svg"


def _find_by_id(root: ET.Element, layer_id: str) -> ET.Element | None:
    for el in root.iter():
        if el.get("id") == layer_id:
            return el
    return None


def _find_by_logical_id(root: ET.Element, logical_id: str) -> ET.Element | None:
    for candidate in (
        _encode_svg_layer_id(logical_id),
        f"_{logical_id}",
        logical_id,
    ):
        hit = _find_by_id(root, candidate)
        if hit is not None:
            return hit
    return None


def _set_visible(el: ET.Element | None, visible: bool) -> None:
    if el is None:
        return
    if visible:
        el.attrib.pop("display", None)
    else:
        el.set("display", "none")


def _set_node_fill(node: ET.Element, hex_color: str) -> None:
    style = node.get("style") or ""
    if "fill:" in style:
        node.set("style", re.sub(r"fill:[^;\"']+", f"fill:{hex_color}", style))
    node.set("fill", hex_color)


def _apply_indicator_colors(root: ET.Element, state: NowPlayingScreenState) -> None:
    for logical_id, attr in _INDICATOR_LAYER_LOGICAL:
        group = _find_by_logical_id(root, logical_id)
        if group is None:
            continue
        ok = bool(getattr(state, attr))
        color = _COLOR_SUCCESS_HEX if ok else _COLOR_FAIL_HEX
        for node in group.iter():
            if node.tag.endswith("circle"):
                _set_node_fill(node, color)


def _remove_element_by_id(root: ET.Element, element_id: str) -> None:
    """Remove a node from the SVG tree (PyMuPDF ignores display:none)."""
    for parent in root.iter():
        for child in list(parent):
            if child.get("id") == element_id:
                parent.remove(child)
                return


def _remove_by_logical_id(root: ET.Element, logical_id: str) -> None:
    for candidate in (
        _encode_svg_layer_id(logical_id),
        f"_{logical_id}",
        logical_id,
    ):
        if _find_by_id(root, candidate) is not None:
            _remove_element_by_id(root, candidate)
            return


def _remove_layers_by_id_substrings(root: ET.Element, substrings: tuple[str, ...]) -> None:
    """Remove Illustrator wrapper groups whose ids still contain demo chrome."""
    removals: list[tuple[ET.Element, ET.Element]] = []
    for parent in root.iter():
        for child in list(parent):
            eid = child.get("id") or ""
            if any(token in eid for token in substrings):
                removals.append((parent, child))
    for parent, child in removals:
        try:
            parent.remove(child)
        except ValueError:
            pass


# Extra patterns for auto-suffixed Illustrator group ids (e.g. volume text wrappers).
_EXTRA_HIDE_ID_SUBSTRINGS: tuple[str, ...] = (
    "_x5F_badge_x5F_container",
    "_x5F_badge_x5F_service",
    "now_playing_service_text",
    "_x5F_service_x5F_text",
    "_x5F_timecode_x5F_container",
    "_x5F_timecode_x5F_text",
    "timecode_stroke",
    "widget_now_playing_stroke",
    "_x5F_content_x5F_time",
    "_x5F_status_x5F_bar_x5F_played",
    "_x5F_status_x5F_bar_x5F_unplayed",
    "_x5F_tmdb_x5F_TT_x5F_normal",
    "_x5F_tmdb_x5F_TT_x5F_black",
    "_x5F_tmdb_x5F_TT_x5F_unplayed",
    "tmdb_TT_played",
    "_x5F_clock_x5F_text",
    "_x5F_audio_x5F_config_x5F_text",
    "_x5F_audio_x5F_config_x5F_volume",
    "audio_config_text",
    "audio_config_volume",
    "audio_levels_lfe_text",
    "audio_levels_LFE_text",
    "audio_levels_sl_text",
    "audio_levels_l_text",
    "audio_levels_c_text",
    "audio_levels_r_text",
    "audio_levels_sr_text",
    "_x5F_backdrop_x5F_tmdb_x5F_backdrop",
    "_x5F_backdrop_x5F_tmdb_x5F_color",
    "_x5F_now_x5F_playing_x5F_color_x5F_two",
    "_x5F_audio_x5F_levels_x5F",
    "_x5F_scale",
)


def _replace_background_with_black(root: ET.Element) -> None:
    """Swap ``07_background`` JPEG art for a flat black fill."""
    markers = ("_x30_7_x5F_background", "_07_background", "07_background")
    for el in root.iter():
        eid = el.get("id") or ""
        if not any(m in eid for m in markers):
            continue
        for child in list(el):
            el.remove(child)
        rect = ET.Element(f"{{{SVG_NS}}}rect")
        rect.set("x", "0")
        rect.set("y", "0")
        rect.set("width", str(int(_SVG_W)))
        rect.set("height", str(int(_SVG_H)))
        rect.set("fill", _COLOR_BG_HEX)
        el.insert(0, rect)
        return


def _decanvas_white_bgra(src: np.ndarray, *, threshold: int = 252) -> np.ndarray:
    """Make PyMuPDF/cairosvg white canvas pixels transparent before compositing."""
    if src is None or src.size == 0 or src.ndim != 3 or src.shape[2] < 4:
        return src
    out = src.copy()
    rgb = out[:, :, :3]
    white = (rgb[:, :, 0] >= threshold) & (rgb[:, :, 1] >= threshold) & (rgb[:, :, 2] >= threshold)
    out[white, 3] = 0
    return out


def apply_now_playing_svg_state(root: ET.Element, state: NowPlayingScreenState) -> None:
    """Mutate SVG before rasterize: strip demo/dynamic layers; recolor status dots."""
    root.set("style", f"background:{_COLOR_BG_HEX}")
    _replace_background_with_black(root)
    for logical_id in _HIDE_LAYER_LOGICAL:
        _remove_by_logical_id(root, logical_id)
    for element_id in _DIRECT_STRIP_SVG_IDS:
        _remove_element_by_id(root, element_id)
    _remove_layers_by_id_substrings(root, _EXTRA_HIDE_ID_SUBSTRINGS)
    _apply_indicator_colors(root, state)


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
    """Return BGRA uint8 (DESIGN_H × DESIGN_W). Uses PyMuPDF; cairosvg if available."""
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

    msg = "Now-playing screen needs PyMuPDF (pip install pymupdf) or cairosvg with system cairo."
    if last_err is not None:
        raise RuntimeError(msg) from last_err
    raise RuntimeError(msg)


def render_now_playing_svg_base_bgra(
    state: NowPlayingScreenState,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    """Load the now-playing SVG, apply ``state`` to static chrome, return 800×480 BGRA."""
    if svg_path is not None:
        path = Path(svg_path)
    else:
        path = default_now_playing_svg_path(assets_dir)
    if not path.is_file():
        raise FileNotFoundError(f"now-playing SVG not found: {path}")

    root = _svg_tree_from_path(path)
    apply_now_playing_svg_state(root, state)
    return _rasterize_svg_tree(root)


@lru_cache(maxsize=8)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Sharp Sans at ``size``; never fall back to Pillow's tiny bitmap default."""
    px = max(6, int(size))
    candidates: list[str] = []
    if path:
        candidates.append(str(path))
    for resolver in (resolve_ui_font_extrabold, resolve_ui_font_bold, resolve_ui_font_medium):
        try:
            p = resolver()
        except Exception:
            p = None
        if p and p not in candidates:
            candidates.append(p)
    for fallback in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ):
        if fallback not in candidates:
            candidates.append(fallback)
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _blend_bgra_onto_bgra(dst: np.ndarray, src: np.ndarray) -> None:
    """Alpha-composite ``src`` onto ``dst`` in place (both BGRA)."""
    if dst is None or src is None or dst.size == 0 or src.size == 0:
        return
    if dst.shape[:2] != src.shape[:2]:
        raise ValueError("blend regions must match")
    src_bgr = src[:, :, :3].astype(np.float32)
    dst_bgr = dst[:, :, :3].astype(np.float32)
    alpha = src[:, :, 3:4].astype(np.float32) / 255.0
    dst[:, :, :3] = np.clip(src_bgr * alpha + dst_bgr * (1.0 - alpha), 0, 255).astype(np.uint8)
    dst[:, :, 3] = np.maximum(dst[:, :, 3], src[:, :, 3])


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


def _rounded_rect_mask_left_only(w: int, h: int, radius: int) -> np.ndarray:
    """Rounded top-left / bottom-left only; right edge stays square for progress crop."""
    if w < 1 or h < 1:
        return np.zeros((max(0, h), max(0, w)), dtype=np.uint8)
    r = max(0, min(radius, min(w, h) // 2))
    mask = np.zeros((h, w), dtype=np.uint8)
    if r <= 0:
        mask[:, :] = 255
        return mask
    cv2.rectangle(mask, (r, 0), (w - 1, h - 1), 255, -1)
    cv2.rectangle(mask, (0, r), (w - 1, h - r - 1), 255, -1)
    cv2.circle(mask, (r, r), r, 255, -1, lineType=cv2.LINE_AA)
    cv2.circle(mask, (r, h - r - 1), r, 255, -1, lineType=cv2.LINE_AA)
    return mask


def _draw_left_rounded_rect_bgra(
    bgra: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    fill_bgr: tuple[int, int, int],
    radius: int = 0,
) -> None:
    if w < 1 or h < 1:
        return
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(DESIGN_W), x + w), min(int(DESIGN_H), y + h)
    if x0 >= x1 or y0 >= y1:
        return
    lw, lh = x1 - x0, y1 - y0
    mask = _rounded_rect_mask_left_only(lw, lh, min(radius, lw // 2, lh // 2))
    patch = np.zeros((lh, lw, 4), dtype=np.uint8)
    patch[:, :, :3] = fill_bgr
    patch[:, :, 3] = mask
    roi = bgra[y0:y1, x0:x1]
    if roi.shape[2] >= 4:
        roi[:, :, :3] = alpha_blend_bgra_over_bgr(roi[:, :, :3], patch)
        roi[:, :, 3] = np.maximum(roi[:, :, 3], patch[:, :, 3])
    else:
        roi[:] = alpha_blend_bgra_over_bgr(roi, patch)


def _stroke_patch_from_mask(
    mask: np.ndarray,
    *,
    stroke_bgr: tuple[int, int, int],
    stroke_px: int,
) -> np.ndarray:
    """Closed outline ring from a filled shape mask (replaces Canny edge strokes)."""
    h, w = mask.shape[:2]
    patch = np.zeros((h, w, 4), dtype=np.uint8)
    if w < 1 or h < 1 or stroke_px < 1:
        return patch
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return patch
    cv2.drawContours(
        patch,
        contours,
        -1,
        (*stroke_bgr, 255),
        thickness=max(1, int(stroke_px)),
        lineType=cv2.LINE_AA,
    )
    return patch


def _composite_stroke_patch_bgra(
    bgra: np.ndarray,
    x: int,
    y: int,
    patch: np.ndarray,
) -> None:
    if patch is None or patch.size == 0:
        return
    _paste_patch_bgra(bgra, patch, x, y)


def _draw_rounded_rect_stroke_bgra(
    bgra: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    stroke_bgr: tuple[int, int, int],
    radius: int = 0,
    stroke: int = 1,
    left_rounded_only: bool = False,
) -> None:
    """Stroke-only rounded rect composited on top."""
    if w < 1 or h < 1 or stroke < 1:
        return
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(DESIGN_W), x + w), min(int(DESIGN_H), y + h)
    if x0 >= x1 or y0 >= y1:
        return
    lw, lh = x1 - x0, y1 - y0
    r = min(radius, lw // 2, lh // 2)
    if left_rounded_only:
        mask = _rounded_rect_mask_left_only(lw, lh, r)
    else:
        mask = _rounded_rect_mask(lw, lh, r)
    patch = _stroke_patch_from_mask(mask, stroke_bgr=stroke_bgr, stroke_px=stroke)
    _composite_stroke_patch_bgra(bgra, x0, y0, patch)


def _draw_rounded_rect_bgra(
    bgra: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    fill_bgr: tuple[int, int, int],
    stroke_bgr: tuple[int, int, int] | None = None,
    radius: int = 0,
    stroke: int = 0,
) -> None:
    if w < 1 or h < 1:
        return
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(DESIGN_W), x + w), min(int(DESIGN_H), y + h)
    if x0 >= x1 or y0 >= y1:
        return
    lw, lh = x1 - x0, y1 - y0
    mask = _rounded_rect_mask(lw, lh, min(radius, lw // 2, lh // 2))
    patch = np.zeros((lh, lw, 4), dtype=np.uint8)
    patch[:, :, :3] = fill_bgr
    patch[:, :, 3] = mask
    if stroke_bgr is not None and stroke > 0:
        edge = cv2.Canny(mask, 50, 150)
        if stroke > 1:
            k = max(1, stroke)
            edge = cv2.dilate(edge, np.ones((k, k), np.uint8))
        patch[edge > 0, :3] = stroke_bgr
        patch[edge > 0, 3] = 255
    roi = bgra[y0:y1, x0:x1]
    if roi.shape[2] >= 4:
        roi[:, :, :3] = alpha_blend_bgra_over_bgr(roi[:, :, :3], patch)
        roi[:, :, 3] = np.maximum(roi[:, :, 3], patch[:, :, 3])
    else:
        roi[:] = alpha_blend_bgra_over_bgr(roi, patch)


def _follow_container_x(container_w: int, bar_l: int, bar_w: int, progress: float) -> int:
    """Badge/timecode container tracks played edge; pins to bar left when too narrow."""
    pf = max(0.0, min(1.0, float(progress)))
    played_right = int(round(float(bar_l) + pf * float(bar_w)))
    played_w = played_right - int(bar_l)
    if played_w >= int(container_w):
        return played_right - int(container_w)
    return int(bar_l)


def _fit_text_patch(
    text: str,
    *,
    size_px: int,
    fill_rgb: tuple[int, int, int],
    bold: bool = True,
    align: str = "left",
    max_width_px: int | None = None,
) -> tuple[np.ndarray, int, int]:
    if not text:
        return np.zeros((1, 1, 4), dtype=np.uint8), 0, 0
    draw_text = str(text)
    if max_width_px is not None and max_width_px > 0:
        probe_patch, probe_w, probe_h = _fit_text_patch(
            draw_text,
            size_px=size_px,
            fill_rgb=fill_rgb,
            bold=bold,
            align=align,
        )
        if probe_w <= max_width_px:
            return probe_patch, probe_w, probe_h
        ell = "…"
        for n in range(len(draw_text), 0, -1):
            candidate = draw_text[:n].rstrip() + ell
            cp, cw, ch = _fit_text_patch(
                candidate,
                size_px=size_px,
                fill_rgb=fill_rgb,
                bold=bold,
                align=align,
            )
            if cw <= max_width_px:
                return cp, cw, ch
        return probe_patch, probe_w, probe_h
    path = resolve_ui_font_extrabold() or resolve_ui_font_bold()
    font = _load_font(str(path or ""), size_px)
    pad = 2
    probe = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    l, t, r, b = draw.textbbox((0, 0), draw_text, font=font)
    tw, th = max(1, r - l), max(1, b - t)
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if align == "right":
        tx = tw + pad - r
    elif align == "center":
        tx = (tw + pad * 2) // 2 - (l + r) // 2
    else:
        tx = pad - l
    ty = pad - t
    draw.text((tx, ty), draw_text, font=font, fill=(*fill_rgb, 255))
    arr = np.asarray(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA), tw + pad * 2, th + pad * 2


def _clock_text(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now()
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{dt.strftime('%M')}{dt.strftime('%p')}"


def _audio_config_line(incoming: str, config: str) -> str:
    """Source format and surround/output mode (e.g. ``MULTI-IN > AURO3D``)."""
    return receiver_audio_config_display_line(incoming, config)


def _paste_text_on_baseline(
    canvas: np.ndarray,
    patch: np.ndarray,
    x: int,
    baseline_y: int,
) -> None:
    """Paste a text patch so its typographic baseline sits on ``baseline_y``."""
    if patch is None or patch.size == 0:
        return
    th = int(patch.shape[0])
    _paste_patch_bgra(canvas, patch, x, baseline_y - th)


def _tt_with_drop_shadow_bgra(src: np.ndarray) -> tuple[np.ndarray, int]:
    """Padded BGRA with a soft shadow behind ``src``; returns ``(image, pad_px)``."""
    if src is None or src.size == 0:
        return src, 0
    ox, oy = _TT_SHADOW_OFFSET
    sigma = float(_TT_SHADOW_BLUR_SIGMA)
    pad = max(8, int(round(sigma * 3)) + max(abs(ox), abs(oy)) + 2)
    h, w = src.shape[:2]
    out_h, out_w = h + pad * 2, w + pad * 2
    shadow = np.zeros((out_h, out_w), dtype=np.float32)
    alpha = src[:, :, 3].astype(np.float32) / 255.0
    sy, sx = pad + oy, pad + ox
    shadow[sy : sy + h, sx : sx + w] = np.maximum(
        shadow[sy : sy + h, sx : sx + w],
        alpha * float(_TT_SHADOW_STRENGTH),
    )
    k = max(3, int(round(sigma * 2)) | 1)
    shadow = cv2.GaussianBlur(shadow, (k, k), sigmaX=sigma, sigmaY=sigma)
    out = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    sa = np.clip(shadow * 255.0, 0.0, 255.0).astype(np.uint8)
    out[:, :, :3] = 0
    out[:, :, 3] = sa
    _paste_patch_bgra(out, src, pad, pad)
    return out, pad


def _tt_to_white_bgra(src: np.ndarray, *, tint: float = _TT_TINT_WHITE) -> np.ndarray:
    """Tint visible TT pixels toward white (``tint``=0.5 → 50% white)."""
    if src is None or src.size == 0:
        return src
    out = src.copy()
    t = max(0.0, min(1.0, float(tint)))
    keep = 1.0 - t
    alpha = out[:, :, 3] > 0
    if np.any(alpha):
        for ch in range(3):
            plane = out[:, :, ch].astype(np.float32)
            plane[alpha] = plane[alpha] * keep + 255.0 * t
            out[:, :, ch] = plane.astype(np.uint8)
    return out


def _bgr_to_bgra(bgr: np.ndarray) -> np.ndarray:
    if bgr is None or bgr.size == 0:
        return bgr
    if bgr.ndim == 3 and bgr.shape[2] == 4:
        return bgr
    if bgr.ndim == 2:
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)


def _paste_rounded_bgra(
    canvas: np.ndarray,
    patch: np.ndarray,
    x: int,
    y: int,
    *,
    radius: int = _IMAGE_CORNER_RX,
) -> None:
    if patch is None or patch.size == 0:
        return
    ph, pw = patch.shape[:2]
    mask = _rounded_rect_mask(pw, ph, radius)
    masked = patch.copy()
    masked[:, :, 3] = cv2.bitwise_and(masked[:, :, 3], mask)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(DESIGN_W), x + pw), min(int(DESIGN_H), y + ph)
    if x0 >= x1 or y0 >= y1:
        return
    sx0, sy0 = x0 - x, y0 - y
    roi = canvas[y0:y1, x0:x1]
    sub = masked[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
    if roi.shape[2] >= 4 and sub.shape[2] >= 4:
        _blend_bgra_onto_bgra(roi, sub)
    else:
        roi[:] = alpha_blend_bgra_over_bgr(roi, sub)


def _fit_tt_in_bar_bgra(src_bgra: np.ndarray, max_w: int, inner_h: int) -> tuple[np.ndarray, int, int]:
    """Scale TT to fit; left-aligned in ``max_w``, vertically centered in ``inner_h``."""
    if src_bgra is None or src_bgra.size == 0 or max_w < 1 or inner_h < 1:
        return np.zeros((max(1, inner_h), max(1, max_w), 4), dtype=np.uint8), 0, 0
    sh, sw = src_bgra.shape[:2]
    if sh < 1 or sw < 1:
        return np.zeros((inner_h, max_w, 4), dtype=np.uint8), 0, 0
    scale = min(max_w / float(sw), inner_h / float(sh))
    tw = max(1, int(round(sw * scale)))
    th = max(1, int(round(sh * scale)))
    resized = cv2.resize(
        src_bgra, (tw, th), interpolation=cv_resize_interp(sw, sh, tw, th)
    )
    out = np.zeros((inner_h, max_w, 4), dtype=np.uint8)
    y0 = max(0, (inner_h - th) // 2)
    out[y0 : y0 + th, :tw] = resized
    nz = out[:, :, 3] > 0
    if np.any(nz):
        ys, xs = np.nonzero(nz)
        ink_w = int(xs.max()) - int(xs.min()) + 1
        ink_h = int(ys.max()) - int(ys.min()) + 1
    else:
        ink_w, ink_h = tw, th
    return out, ink_w, ink_h


def _layout_tt_and_backdrop_rects(
    tt_bgra: np.ndarray | None,
    backdrop_bgr: np.ndarray | None,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int], np.ndarray | None, np.ndarray | None]:
    """Return (tt_xywh, bd_xywh) in design coords plus fitted patches."""
    pad = _CONTENT_PAD
    inner_x = _BAR_L + pad
    inner_y = _BAR_T + pad
    inner_w = max(8, _BAR_W - 2 * pad)
    inner_h = max(8, _BAR_H - 2 * pad)
    gap = pad
    min_bd_w = max(48, inner_w // 3)
    tt_fit = None
    tt_w, tt_h = 0, 0
    if tt_bgra is not None and tt_bgra.size > 0:
        trimmed = _trim_visible_bgra(tt_bgra)
        max_tt_w = max(8, inner_w - gap - min_bd_w)
        tt_fit, tt_w, tt_h = _fit_tt_in_bar_bgra(trimmed, max_tt_w, inner_h)
    tt_x = inner_x
    tt_y = inner_y
    bd_x = tt_x + tt_w + gap
    bd_y = inner_y
    bd_w = max(8, inner_x + inner_w - bd_x)
    bd_h = inner_h
    bd_fit = None
    if backdrop_bgr is not None and backdrop_bgr.size > 0:
        bd_fit = _image_contain_center_bgra(_bgr_to_bgra(backdrop_bgr), bd_w, bd_h)
    return (tt_x, tt_y, max(tt_w, 0), max(tt_h, 0)), (bd_x, bd_y, bd_w, bd_h), tt_fit, bd_fit


# Luma at/above this becomes pure white in the unplayed duotone. Binary cut —
# a pixel is either full white or on the red/black ramp, never a gray ramp.
_UNPLAYED_HIGHLIGHT_LUMA_MIN = 235


def _prepare_backdrop_unplayed_bgra(bd_fit: np.ndarray) -> np.ndarray:
    """Red duotone for the unplayed backdrop; only the brightest highlights pop to pure white."""
    if bd_fit is None or bd_fit.size == 0:
        return bd_fit
    out = bd_fit.copy()
    mask = out[:, :, 3] > 0
    if not np.any(mask):
        return out
    mono = bgr_to_red_monochrome_luma(out[:, :, :3])
    highlights = mono[:, :, 2] >= _UNPLAYED_HIGHLIGHT_LUMA_MIN
    out[:, :, :3] = mono
    out[:, :, :3][highlights] = 255
    return out


def _compose_bar_group_bgra(
    *,
    played: bool,
    tt_bgra: np.ndarray | None,
    backdrop_bgr: np.ndarray | None,
) -> np.ndarray:
    """Full-width played or unplayed now-playing group (``_BAR_W`` × ``_BAR_H``)."""
    canvas = np.zeros((_BAR_H, _BAR_W, 4), dtype=np.uint8)
    fill = _COLOR_PLAYED_BGR if played else _COLOR_UNPLAYED_BGR
    if played:
        _draw_left_rounded_rect_bgra(
            canvas, 0, 0, _BAR_W, _BAR_H, fill_bgr=fill, radius=_BAR_RX,
        )
    else:
        _draw_rounded_rect_bgra(
            canvas, 0, 0, _BAR_W, _BAR_H, fill_bgr=fill, radius=_BAR_RX,
        )
    (tt_x, tt_y, _, _), (bd_x, bd_y, _, _), tt_fit, bd_fit = _layout_tt_and_backdrop_rects(
        tt_bgra, backdrop_bgr
    )
    rel_tt_x = tt_x - _BAR_L
    rel_tt_y = tt_y - _BAR_T
    rel_bd_x = bd_x - _BAR_L
    rel_bd_y = bd_y - _BAR_T
    if tt_fit is not None and tt_fit.size > 0:
        if played:
            tt_layer = tt_fit
            if _TT_PLAYED_DROP_SHADOW:
                shadowed, spad = _tt_with_drop_shadow_bgra(tt_layer)
                _paste_patch_bgra(canvas, shadowed, rel_tt_x - spad, rel_tt_y - spad)
            else:
                _paste_rounded_bgra(canvas, tt_layer, rel_tt_x, rel_tt_y, radius=_IMAGE_CORNER_RX)
        else:
            tt_layer = _tt_to_white_bgra(tt_fit)
            _paste_rounded_bgra(canvas, tt_layer, rel_tt_x, rel_tt_y, radius=_IMAGE_CORNER_RX)
    if bd_fit is not None and bd_fit.size > 0:
        bd_layer = bd_fit if played else _prepare_backdrop_unplayed_bgra(bd_fit)
        _paste_rounded_bgra(canvas, bd_layer, rel_bd_x, rel_bd_y, radius=_IMAGE_CORNER_RX)
    return canvas


def _compose_played_stroke_bgra(played_w: int) -> np.ndarray | None:
    """White outline around the played bar pill (left-rounded, width = progress)."""
    if played_w <= 0:
        return None
    pw = max(1, min(int(played_w), _BAR_W))
    r = min(_BAR_RX, pw // 2, _BAR_H // 2)
    mask = _rounded_rect_mask_left_only(pw, _BAR_H, r)
    return _stroke_patch_from_mask(
        mask,
        stroke_bgr=_COLOR_ACCENT_BGR,
        stroke_px=_PLAYED_STROKE_PX,
    )


class _AudioLevelsSimulator:
    """Smooth non-repeating vertical meter simulation for development."""

    # Time constant for exponential easing toward the target (seconds).
    _TAU_S = 0.09

    def __init__(self) -> None:
        self._targets: dict[str, float] = {name: 0.2 for name, *_ in _AUDIO_CHANNELS}
        self._current: dict[str, float] = dict(self._targets)
        self._next_jump = 0.0
        self._last_t: float | None = None
        self._rng = random.Random()

    def levels(self, t: float) -> dict[str, float]:
        if t >= self._next_jump:
            for name, *_ in _AUDIO_CHANNELS:
                self._targets[name] = self._rng.uniform(0.04, 1.0)
            self._next_jump = t + self._rng.uniform(0.14, 0.45)
        dt = 1.0 / 30.0 if self._last_t is None else max(0.0, min(0.25, t - self._last_t))
        self._last_t = t
        # Frame-rate independent smoothing: same visual speed at any composite cadence.
        k = 1.0 - math.exp(-dt / self._TAU_S)
        for name, *_ in _AUDIO_CHANNELS:
            cur = self._current[name]
            tgt = self._targets[name]
            self._current[name] = cur + (tgt - cur) * k
        return dict(self._current)


def _paste_patch_bgra(canvas: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
    if patch is None or patch.size == 0:
        return
    ph, pw = patch.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(int(DESIGN_W), x + pw), min(int(DESIGN_H), y + ph)
    if x0 >= x1 or y0 >= y1:
        return
    sx0, sy0 = x0 - x, y0 - y
    roi = canvas[y0:y1, x0:x1]
    sub = patch[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
    if roi.shape[2] >= 4 and sub.shape[2] >= 4:
        _blend_bgra_onto_bgra(roi, sub)
    else:
        roi[:] = alpha_blend_bgra_over_bgr(roi, sub)


def _draw_audio_channel_labels_bgra(canvas: np.ndarray) -> None:
    """Draw Sharp Sans Extrabold channel labels on the lower baseline row."""
    size_px = max(10, _sy(20.0))
    fill_rgb = (237, 28, 36)
    for label, x, baseline_y in _AUDIO_LABELS:
        patch, _, _ = _fit_text_patch(label, size_px=size_px, fill_rgb=fill_rgb, bold=True)
        _paste_text_on_baseline(canvas, patch, x, baseline_y)


def _wipe_bottom_receiver_text_bands_bgra(canvas: np.ndarray) -> None:
    """Clear stale audio/volume/service ink without erasing channel labels, clock, or status dots."""
    bottom = int(DESIGN_H)
    # Config row (below SL/L/C/R/SR/LFE labels).
    cfg_h = max(0, bottom - _BOTTOM_CONFIG_WIPE_TOP)
    if cfg_h > 0:
        _draw_rounded_rect_bgra(
            canvas,
            0,
            _BOTTOM_CONFIG_WIPE_TOP,
            300,
            cfg_h,
            fill_bgr=_COLOR_BG_BGR,
            radius=0,
        )
    # Volume + service band (stops before indicator column).
    mid_h = max(0, bottom - _BOTTOM_MID_WIPE_TOP)
    mid_w = max(0, _BOTTOM_TEXT_WIPE_RIGHT_X - 240)
    if mid_h > 0 and mid_w > 0:
        _draw_rounded_rect_bgra(
            canvas,
            240,
            _BOTTOM_MID_WIPE_TOP,
            mid_w,
            mid_h,
            fill_bgr=_COLOR_BG_BGR,
            radius=0,
        )


def _draw_status_indicator_dots_bgra(canvas: np.ndarray, state: NowPlayingScreenState) -> None:
    """Redraw the four status dots after bottom wipes (SVG ink is cleared in that pass)."""
    flags = (
        state.indicator_audio,
        state.indicator_now_playing,
        state.indicator_receiver,
        state.indicator_tmdb,
    )
    r = _STATUS_INDICATOR_DOT_R
    for (cx, cy), ok in zip(_STATUS_INDICATOR_CENTERS, flags):
        bgr = _COLOR_SUCCESS_BGR if ok else _COLOR_FAIL_BGR
        cv2.circle(
            canvas,
            (int(round(cx)), int(round(cy))),
            r,
            (*bgr, 255),
            thickness=-1,
            lineType=cv2.LINE_AA,
        )


def _draw_audio_levels_bgra(
    canvas: np.ndarray,
    levels: dict[str, float],
) -> None:
    for name, x, baseline, bar_w, max_h in _AUDIO_CHANNELS:
        lv = max(0.0, min(1.0, float(levels.get(name, 0.0))))
        if lv <= 0.0:
            continue
        h = max(1, int(round(max_h * lv)))
        y0 = baseline - h
        if y0 < 0:
            continue
        patch = np.zeros((h, bar_w, 4), dtype=np.uint8)
        patch[:, :, :3] = _COLOR_UI_BGR
        patch[:, :, 3] = 255
        _paste_rounded_bgra(canvas, patch, x, y0, radius=2)


def _trim_visible_bgra(src: np.ndarray, *, alpha_threshold: int = 8) -> np.ndarray:
    """Crop to non-transparent ink bounds (ignores side padding in cached TT assets)."""
    if src is None or src.size == 0:
        return src
    if src.ndim != 3 or src.shape[2] < 4:
        return src
    nz = src[:, :, 3] > alpha_threshold
    if not np.any(nz):
        return src
    ys, xs = np.nonzero(nz)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return src[y0:y1, x0:x1]


def status_bar_slot_wh() -> tuple[int, int]:
    """Design-pixel (w, h) of the now-playing bar."""
    return (_BAR_W, _BAR_H)


def tt_slot_design_x() -> int:
    return _BAR_L


def _reveal_crop_bgra_left(full: np.ndarray, reveal_w: int) -> np.ndarray | None:
    """Return the left ``reveal_w`` columns of a full-size layer (square right crop edge)."""
    if full is None or full.size == 0 or reveal_w <= 0:
        return None
    rw = max(0, min(int(reveal_w), int(full.shape[1])))
    if rw <= 0:
        return None
    return full[:, :rw].copy()


def _fallback_base_bgra() -> np.ndarray:
    """Flat background when SVG rasterization is unavailable."""
    out = np.zeros((int(DESIGN_H), int(DESIGN_W), 4), dtype=np.uint8)
    out[:, :, :3] = _COLOR_BG_BGR
    out[:, :, 3] = 255
    return out


class NowPlayingScreenWidget:
    """Self-contained now-playing layout for DisplayView.ONE (replaces status bar + overlay + clock)."""

    def __init__(self, *, assets_dir: Path) -> None:
        self._assets_dir = Path(assets_dir)
        self._state = NowPlayingScreenState()
        self._backdrop_bgr: np.ndarray | None = None
        self._tt_bgra: np.ndarray | None = None
        self._audio_sim = _AudioLevelsSimulator()
        self._cached_bgra: np.ndarray | None = None
        self._cached_sig: tuple[object, ...] | None = None
        # Rasterized + de-whitened SVG chrome; only indicators / the file itself change it.
        self._svg_chrome_bgra: np.ndarray | None = None
        self._svg_chrome_sig: tuple[object, ...] | None = None
        # Fully-composited frame minus the animated meter bars; reused across
        # audio-sim ticks so each tick only copies + redraws the meters.
        self._static_bgra: np.ndarray | None = None
        self._static_sig: tuple[object, ...] | None = None

    @property
    def chrome_visible(self) -> bool:
        return self._state.chrome_visible

    def clear_cache(self) -> None:
        self._cached_bgra = None
        self._cached_sig = None
        self._static_bgra = None
        self._static_sig = None

    def set_now_playing_chrome_visible(self, visible: bool) -> bool:
        v = bool(visible)
        if v == self._state.chrome_visible:
            return False
        self._state.chrome_visible = v
        self.clear_cache()
        return True

    def set_trt_substantive(self, substantive: bool) -> bool:
        v = bool(substantive)
        if v == self._state.trt_substantive:
            return False
        self._state.trt_substantive = v
        self.clear_cache()
        return True

    def set_theater_dim_suppressed(self, suppressed: bool) -> bool:
        v = bool(suppressed)
        if v == self._state.theater_dim_suppressed:
            return False
        self._state.theater_dim_suppressed = v
        self.clear_cache()
        return True

    def set_now_playing_display(
        self,
        *,
        remaining_text: str | None = None,
        progress: float | None = None,
        show_paused: bool | None = None,
    ) -> bool:
        changed = False
        if remaining_text is not None:
            t = str(remaining_text)
            if t != self._state.remaining_text:
                self._state.remaining_text = t
                changed = True
        if progress is not None:
            pf = max(0.0, min(1.0, float(progress)))
            if abs(pf - self._state.progress) > 1e-9:
                self._state.progress = pf
                changed = True
        if show_paused is not None:
            sp = bool(show_paused)
            if sp != self._state.show_paused:
                self._state.show_paused = sp
                changed = True
        if changed:
            self.clear_cache()
        return changed

    def set_receiver_state(self, *, incoming: str, config: str, volume: str) -> bool:
        changed = False
        for key, val in (("incoming", incoming), ("config", config), ("volume", volume)):
            if _looks_like_receiver_debug_blob(val):
                s = ""
            else:
                s = str(val or "")
            if s != getattr(self._state, key):
                setattr(self._state, key, s)
                changed = True
        if changed:
            self.clear_cache()
        return changed

    def set_audio_levels_sim(self, enabled: bool) -> bool:
        v = bool(enabled)
        if v == self._state.audio_levels_sim:
            return False
        self._state.audio_levels_sim = v
        self.clear_cache()
        return True

    def set_layout_mode(self, mode: str) -> bool:
        m = _normalized_layout_mode(mode)
        if m == self._state.layout_mode:
            return False
        self._state.layout_mode = m
        self.clear_cache()
        return True

    def set_streaming_badge(self, *, show: bool, filename: str, label: str) -> bool:
        sig = (bool(show), str(filename or ""), str(label or ""))
        cur = (self._state.badge_show, self._state.badge_filename, self._state.badge_label)
        if sig == cur:
            return False
        self._state.badge_show, self._state.badge_filename, self._state.badge_label = sig
        self.clear_cache()
        return True

    def set_indicators(
        self,
        *,
        now_playing: bool,
        receiver: bool,
        tmdb: bool,
        audio: bool = False,
    ) -> bool:
        sig = (bool(now_playing), bool(receiver), bool(tmdb), bool(audio))
        cur = (
            self._state.indicator_now_playing,
            self._state.indicator_receiver,
            self._state.indicator_tmdb,
            self._state.indicator_audio,
        )
        if sig == cur:
            return False
        (
            self._state.indicator_now_playing,
            self._state.indicator_receiver,
            self._state.indicator_tmdb,
            self._state.indicator_audio,
        ) = sig
        self.clear_cache()
        return True

    def set_backdrop_bgr(self, backdrop_bgr: np.ndarray | None) -> bool:
        if backdrop_bgr is None:
            if self._backdrop_bgr is None:
                return False
            self._backdrop_bgr = None
            self.clear_cache()
            return True
        arr = np.asarray(backdrop_bgr, dtype=np.uint8)
        if self._backdrop_bgr is not None and self._backdrop_bgr.shape == arr.shape:
            if np.array_equal(self._backdrop_bgr, arr):
                return False
        self._backdrop_bgr = arr.copy()
        self.clear_cache()
        return True

    def set_tt_bgra(self, tt_bgra: np.ndarray | None) -> bool:
        if tt_bgra is None:
            if self._tt_bgra is None:
                return False
            self._tt_bgra = None
            self.clear_cache()
            return True
        arr = np.asarray(tt_bgra, dtype=np.uint8)
        if self._tt_bgra is not None and self._tt_bgra.shape == arr.shape:
            if np.array_equal(self._tt_bgra, arr):
                return False
        self._tt_bgra = arr.copy()
        self.clear_cache()
        return True

    def update_state(
        self,
        *,
        progress: float,
        remaining_text: str,
        played_text: str,
        incoming_audio: str,
        playback_config: str,
        volume_text: str,
        has_now_playing: bool,
        has_receiver: bool,
        has_tmdb: bool,
        audio_analysis: bool,
        service_badge_bgra: np.ndarray | None,
        tmdb_tt_bgra: np.ndarray | None,
        tmdb_backdrop_bgr: np.ndarray | None,
        show_paused: bool = False,
        trt_substantive: bool = True,
        theater_dim_suppressed: bool = False,
        badge_show: bool = False,
        badge_filename: str = "",
        badge_label: str = "",
        audio_levels_sim: bool | None = None,
        layout_mode: str | None = None,
        indicator_now_playing: bool | None = None,
    ) -> bool:
        """Batch update from ``pigeon_0_7`` holders; returns True when the cached frame is stale."""
        changed = False
        if self.set_now_playing_chrome_visible(has_now_playing):
            changed = True
        if self.set_trt_substantive(trt_substantive):
            changed = True
        if self.set_theater_dim_suppressed(theater_dim_suppressed):
            changed = True
        if self.set_now_playing_display(
            remaining_text=str(remaining_text or ""),
            progress=float(progress),
            show_paused=bool(show_paused),
        ):
            changed = True
        if self.set_receiver_state(
            incoming=str(incoming_audio or ""),
            config=str(playback_config or ""),
            volume=str(volume_text or ""),
        ):
            changed = True
        if self.set_indicators(
            now_playing=bool(
                has_now_playing
                if indicator_now_playing is None
                else indicator_now_playing
            ),
            receiver=bool(has_receiver),
            tmdb=bool(has_tmdb),
            audio=bool(audio_analysis),
        ):
            changed = True
        if layout_mode is not None and self.set_layout_mode(layout_mode):
            changed = True
        if self.set_backdrop_bgr(tmdb_backdrop_bgr):
            changed = True
        if self.set_tt_bgra(tmdb_tt_bgra):
            changed = True
        if self.set_streaming_badge(
            show=bool(badge_show),
            filename=str(badge_filename or ""),
            label=str(badge_label or ""),
        ):
            changed = True
        _ = service_badge_bgra  # service label at ``05_now_playing_service_text`` (below clock)
        if audio_levels_sim is not None and self.set_audio_levels_sim(bool(audio_levels_sim)):
            changed = True
        _ = played_text  # elapsed shown via progress bar width only in this layout
        return changed

    def _state_sig(self) -> tuple[object, ...]:
        st = self._state
        bd_id = id(self._backdrop_bgr) if self._backdrop_bgr is not None else None
        tt_id = id(self._tt_bgra) if self._tt_bgra is not None else None
        return (
            round(st.progress, 6),
            st.remaining_text,
            st.show_paused,
            st.chrome_visible,
            st.trt_substantive,
            st.theater_dim_suppressed,
            st.incoming,
            st.config,
            st.volume,
            st.badge_show,
            st.badge_filename,
            st.badge_label,
            st.indicator_now_playing,
            st.indicator_receiver,
            st.indicator_tmdb,
            st.indicator_audio,
            st.audio_levels_sim,
            st.layout_mode,
            bd_id,
            tt_id,
            # Clock shows h:mm only — invalidate per minute, not per second.
            int(datetime.now().strftime("%H%M")),
            int(time.monotonic() * 30) if st.audio_levels_sim else 0,
        )

    def _paste_patch(self, canvas: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
        _paste_patch_bgra(canvas, patch, x, y)

    def _svg_chrome_cache_sig(self) -> tuple[object, ...]:
        st = self._state
        path = default_now_playing_svg_path(self._assets_dir)
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = -1
        return (
            str(path),
            mtime,
            st.indicator_now_playing,
            st.indicator_receiver,
            st.indicator_tmdb,
            st.indicator_audio,
        )

    def _render_svg_base(self) -> np.ndarray:
        """Rasterized + de-whitened SVG chrome, cached until indicators or the file change."""
        sig = self._svg_chrome_cache_sig()
        if self._svg_chrome_bgra is not None and self._svg_chrome_sig == sig:
            return self._svg_chrome_bgra
        try:
            base = render_now_playing_svg_base_bgra(self._state, assets_dir=self._assets_dir)
        except (FileNotFoundError, RuntimeError, ImportError):
            base = _fallback_base_bgra()
        self._svg_chrome_bgra = _decanvas_white_bgra(base)
        self._svg_chrome_sig = sig
        return self._svg_chrome_bgra

    def _render_static_bgra(self) -> np.ndarray:
        """Everything except the animated audio meter bars (cached across sim ticks)."""
        mode = _normalized_layout_mode(self._state.layout_mode)
        if mode == "full":
            return self._render_static_full_bgra()
        return self._render_static_variation_bgra(mode)

    def _draw_bar_and_timecode(self, out: np.ndarray) -> None:
        """Now-playing bar (unplayed + played reveal + paused overlay) and timecode pill."""
        st = self._state
        progress = st.progress if st.trt_substantive else 0.0
        played_w = max(0, min(_BAR_W, int(round(progress * float(_BAR_W)))))

        # During pause, keep TMDb TT + backdrop visible under the "paused" overlay.
        bar_tt = self._tt_bgra
        bar_backdrop = self._backdrop_bgr

        unplayed_group = _compose_bar_group_bgra(
            played=False,
            tt_bgra=bar_tt,
            backdrop_bgr=bar_backdrop,
        )
        self._paste_patch(out, unplayed_group, _BAR_L, _BAR_T)

        if played_w > 0:
            played_group = _compose_bar_group_bgra(
                played=True,
                tt_bgra=bar_tt,
                backdrop_bgr=bar_backdrop,
            )
            played_crop = _reveal_crop_bgra_left(played_group, played_w)
            if played_crop is not None:
                self._paste_patch(out, played_crop, _BAR_L, _BAR_T)
            stroke_crop = _compose_played_stroke_bgra(played_w)
            if stroke_crop is not None:
                self._paste_patch(out, stroke_crop, _BAR_L, _BAR_T)

        if st.show_paused:
            pad_x = max(3, int(round(_BAR_W * 0.07)))
            pad_y = max(3, int(round(_BAR_H * 0.18)))
            pw = max(8, _BAR_W - 2 * pad_x)
            ph = max(8, _BAR_H - 2 * pad_y)
            px = _BAR_L + pad_x
            py = _BAR_T + pad_y
            _paused_pad = max(4, int(round(min(pw, ph) * 0.09)))
            paused = _text_patch_bgra(
                "paused",
                pw,
                ph,
                align="center",
                fill_rgba=(255, 255, 255, 230),
                fit_max_h=min(56, max(28, int(round(0.26 * float(_BAR_H))))),
                edge_pad_px=int(_paused_pad),
            )
            self._paste_patch(out, paused, px, py)

        container_w = _CONTAINER_W
        tc_x = _follow_container_x(container_w, _BAR_L, _BAR_W, progress)
        _draw_rounded_rect_bgra(
            out,
            tc_x,
            _TC_Y,
            _TC_W,
            _TC_H,
            fill_bgr=_COLOR_PLAYED_BGR,
            radius=_CONTAINER_RX,
        )
        _draw_rounded_rect_stroke_bgra(
            out,
            tc_x,
            _TC_Y,
            _TC_W,
            _TC_H,
            stroke_bgr=_COLOR_ACCENT_BGR,
            radius=_CONTAINER_RX,
            stroke=_PLAYED_STROKE_PX,
        )
        tc_text = str(st.remaining_text or "").strip()
        if tc_text:
            tc_patch, tw, th = _fit_text_patch(
                tc_text,
                size_px=max(10, _sy(30.0)),
                fill_rgb=(255, 255, 255),
                bold=True,
                align="center",
            )
            tx = tc_x + max(0, (_TC_W - tw) // 2)
            ty = _TC_Y + max(0, (_TC_H - th) // 2)
            self._paste_patch(out, tc_patch, tx, ty)

    def _render_static_full_bgra(self) -> np.ndarray:
        """070326 SVG layout with audio meter chrome (HDMI analysis / sim)."""
        st = self._state
        out = _fallback_base_bgra()
        self._paste_patch(out, self._render_svg_base(), 0, 0)

        self._draw_bar_and_timecode(out)

        # Targeted bottom wipes — full-width band was erasing status dots and clock ink.
        _wipe_bottom_receiver_text_bands_bgra(out)

        _draw_audio_channel_labels_bgra(out)

        clk_patch, clk_tw, _ = _fit_text_patch(
            _clock_text(),
            size_px=_CLOCK_SIZE_PX,
            fill_rgb=(237, 28, 36),
            bold=True,
            align="right",
        )
        clk_x = _CLOCK_RIGHT_X - clk_tw

        vol_line = _receiver_volume_display_line(st.volume)
        vol_center_x = (_AUDIO_METER_RIGHT_X + clk_x) // 2
        if vol_line:
            vol_patch, vol_tw, _ = _fit_text_patch(
                vol_line,
                size_px=max(10, _sy(float(_VOLUME_SIZE_PX))),
                fill_rgb=(225, 0, 24),
                bold=True,
            )
            vol_x = vol_center_x - vol_tw // 2
            _paste_text_on_baseline(out, vol_patch, vol_x, _UPPER_BASELINE_Y)
            vol_center_x = vol_x + vol_tw // 2

        cfg_line = _audio_config_line(st.incoming, st.config)
        if cfg_line:
            cfg_patch, cfg_tw, _ = _fit_text_patch(
                cfg_line,
                size_px=max(10, _sy(float(_AUDIO_CFG_TEXT_SIZE))),
                fill_rgb=(237, 28, 36),
                bold=True,
                max_width_px=_AUDIO_CFG_MAX_W,
            )
            cfg_x = vol_center_x - cfg_tw // 2
            _paste_text_on_baseline(out, cfg_patch, cfg_x, _LOWER_BASELINE_Y)

        service_text = str(st.badge_label or "").strip()
        if service_text:
            svc_patch, svc_tw, _ = _fit_text_patch(
                service_text.lower(),
                size_px=max(10, _sy(float(_SERVICE_TEXT_SIZE_PX))),
                fill_rgb=(237, 28, 36),
                bold=True,
                align="right",
            )
            svc_x = _SERVICE_TEXT_RIGHT_X - svc_tw
            _paste_text_on_baseline(out, svc_patch, svc_x, _LOWER_BASELINE_Y)

        # Clock + status dots last so bottom wipes never cover them.
        _paste_text_on_baseline(out, clk_patch, clk_x, _UPPER_BASELINE_Y)
        _draw_status_indicator_dots_bgra(out, st)

        return out

    def _render_static_variation_bgra(self, mode: str) -> np.ndarray:
        """Meter-less layouts (np/rv/ck mocks): black base, no SVG chrome, dots always drawn."""
        st = self._state
        out = _fallback_base_bgra()

        has_bar = mode in ("np_rv_ck", "np_ck")
        show_receiver_text = mode in ("np_rv_ck", "rv_ck")

        if has_bar:
            self._draw_bar_and_timecode(out)

        vol_line = _receiver_volume_display_line(st.volume) if show_receiver_text else ""
        cfg_line = _audio_config_line(st.incoming, st.config) if show_receiver_text else ""
        cfg_line = cfg_line.lower()
        service_text = str(st.badge_label or "").strip().lower() if has_bar else ""

        if mode == "np_rv_ck":
            # Left cluster: volume over audio config, aligned with the bar's left edge.
            if vol_line:
                vol_patch, _, _ = _fit_text_patch(
                    vol_line,
                    size_px=max(10, _sy(float(_NP_VOLUME_SIZE_PX))),
                    fill_rgb=(225, 0, 24),
                    bold=True,
                )
                _paste_text_on_baseline(out, vol_patch, _NP_LEFT_X, _NP_UPPER_BASELINE_Y)
            if cfg_line:
                cfg_patch, _, _ = _fit_text_patch(
                    cfg_line,
                    size_px=max(10, _sy(float(_NP_TEXT_SIZE_PX))),
                    fill_rgb=(237, 28, 36),
                    bold=True,
                    max_width_px=_AUDIO_CFG_MAX_W,
                )
                _paste_text_on_baseline(out, cfg_patch, _NP_LEFT_X, _NP_LOWER_BASELINE_Y)

        if has_bar:
            clk_patch, clk_tw, _ = _fit_text_patch(
                _clock_text(),
                size_px=max(10, _sy(float(_NP_CLOCK_SIZE_PX))),
                fill_rgb=(237, 28, 36),
                bold=True,
                align="right",
            )
            if mode == "np_ck":
                clk_x = (int(DESIGN_W) - clk_tw) // 2
            else:
                clk_x = _CLOCK_RIGHT_X - clk_tw
            _paste_text_on_baseline(out, clk_patch, clk_x, _NP_UPPER_BASELINE_Y)
            if service_text:
                svc_patch, svc_tw, _ = _fit_text_patch(
                    service_text,
                    size_px=max(10, _sy(float(_NP_TEXT_SIZE_PX))),
                    fill_rgb=(237, 28, 36),
                    bold=True,
                    align="right",
                )
                if mode == "np_ck":
                    svc_x = clk_x + (clk_tw - svc_tw) // 2
                else:
                    svc_x = _NP_SERVICE_RIGHT_X - svc_tw
                _paste_text_on_baseline(out, svc_patch, svc_x, _NP_LOWER_BASELINE_Y)
        else:
            # rv_ck / ck: right-aligned stack — volume, config, then a large clock.
            if vol_line:
                vol_patch, vol_tw, _ = _fit_text_patch(
                    vol_line,
                    size_px=max(10, _sy(float(_RV_VOLUME_SIZE_PX))),
                    fill_rgb=(225, 0, 24),
                    bold=True,
                    align="right",
                )
                _paste_text_on_baseline(
                    out, vol_patch, _RV_RIGHT_X - vol_tw, _RV_VOLUME_BASELINE_Y
                )
            if cfg_line:
                cfg_patch, cfg_tw, _ = _fit_text_patch(
                    cfg_line,
                    size_px=max(10, _sy(float(_RV_CFG_SIZE_PX))),
                    fill_rgb=(237, 28, 36),
                    bold=True,
                    align="right",
                )
                _paste_text_on_baseline(
                    out, cfg_patch, _RV_RIGHT_X - cfg_tw, _RV_CFG_BASELINE_Y
                )
            clk_patch, clk_tw, _ = _fit_text_patch(
                _clock_text(),
                size_px=max(10, _sy(float(_RV_CLOCK_SIZE_PX))),
                fill_rgb=(237, 28, 36),
                bold=True,
                align="right",
            )
            _paste_text_on_baseline(
                out, clk_patch, _RV_RIGHT_X - clk_tw, _RV_CLOCK_BASELINE_Y
            )

        _draw_status_indicator_dots_bgra(out, st)
        return out

    def _render_frame_bgra(self, sig: tuple[object, ...] | None = None) -> np.ndarray:
        st = self._state
        if sig is None:
            sig = self._state_sig()
        # The last sig element is the audio-sim time bucket; everything else
        # describes the static (non-meter) content.
        static_sig = sig[:-1]
        if self._static_bgra is None or self._static_sig != static_sig:
            self._static_bgra = self._render_static_bgra()
            self._static_sig = static_sig

        # Animated meters exist only in the full (HDMI-analysis) layout.
        if not st.audio_levels_sim or _normalized_layout_mode(st.layout_mode) != "full":
            return self._static_bgra
        out = self._static_bgra.copy()
        _draw_audio_levels_bgra(out, self._audio_sim.levels(time.monotonic()))
        return out

    def bgra_frame(self) -> np.ndarray | None:
        if (
            not self._state.chrome_visible
            or self._state.theater_dim_suppressed
        ):
            return None
        sig = self._state_sig()
        if self._cached_bgra is not None and self._cached_sig == sig:
            return self._cached_bgra
        self._cached_sig = sig
        self._cached_bgra = self._render_frame_bgra(sig)
        return self._cached_bgra

    def design_blits(self) -> list[DesignPatch]:
        frame = self.bgra_frame()
        if frame is None:
            return []
        return [
            DesignPatch(
                x=0,
                y=0,
                w=int(DESIGN_W),
                h=int(DESIGN_H),
                bgra=frame,
                layer="now_playing_screen",
            )
        ]

    def render(self, canvas_bgr: np.ndarray) -> None:
        for patch in self.design_blits():
            x, y, w, h = patch.x, patch.y, patch.w, patch.h
            if w < 1 or h < 1:
                continue
            x0 = max(0, x)
            y0 = max(0, y)
            x1 = min(canvas_bgr.shape[1], x + w)
            y1 = min(canvas_bgr.shape[0], y + h)
            if x0 >= x1 or y0 >= y1:
                continue
            sx0, sy0 = x0 - x, y0 - y
            roi = canvas_bgr[y0:y1, x0:x1]
            sub = patch.bgra[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
            roi[:] = alpha_blend_bgra_over_bgr(roi, sub)


def sync_now_playing_screen_indicators(
    widget: NowPlayingScreenWidget | None,
    *,
    now_playing: bool,
    receiver: bool,
    tmdb: bool,
) -> bool:
    if widget is None:
        return False
    return widget.set_indicators(
        now_playing=now_playing,
        receiver=receiver,
        tmdb=tmdb,
        audio=False,
    )
