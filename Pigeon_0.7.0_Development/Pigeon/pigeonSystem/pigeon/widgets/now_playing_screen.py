"""
Pigeon 0.7 now-playing screen — SVG layer chrome from ``now_playing_test_070126`` (800×480 → 800×400).

Static chrome is rasterized from ``pigeonAssets/now_playing_test_070126.svg`` (see
:func:`render_now_playing_svg_base_bgra`). Dynamic layers (TMDb backdrop/TT, streaming badge,
timecodes, progress, live text) are drawn programmatically on top.
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.font_paths import resolve_ui_font_bold, resolve_ui_font_extrabold, resolve_ui_font_medium
from pigeon.widgets.playback_overlay import (
    _image_contain_center_bgra,
    _receiver_audio_display_line,
    _receiver_volume_display_line,
    _text_patch_bgra,
)
from pigeon.widgets.status_bar import DesignPatch

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# --- Colors (Numbers spec) ---
_COLOR_UI_HEX = "#E10018"
_COLOR_ACCENT_HEX = "#FFFFFF"
_COLOR_BG_HEX = "#202020"
_COLOR_SUCCESS_HEX = "#01D800"
_COLOR_FAIL_HEX = "#E10018"
_COLOR_UNPLAYED_HEX = "#282828"

_COLOR_UI_BGR = (24, 0, 225)
_COLOR_ACCENT_BGR = (255, 255, 255)
_COLOR_BG_BGR = (32, 32, 32)
_COLOR_SUCCESS_BGR = (0, 216, 1)
_COLOR_FAIL_BGR = (24, 0, 225)
_COLOR_UNPLAYED_BGR = (40, 40, 40)

_SVG_W = 800.0
_SVG_H = 480.0
_Y_SCALE = float(DESIGN_H) / _SVG_H

# Logical Illustrator layer ids (encoded in SVG via _encode_svg_layer_id).
_HIDE_LAYER_LOGICAL: tuple[str, ...] = (
    "04_widget_backdrop_tmdb_backdrop",
    "03_widget_now_playing_tmdb_TT_black",
    "03_widget_now_playing_tmdb_TT_normal",
    "03_widget_now_playing_status_bar_played",
    "03_widget_now_playing_badge_service",
    "03_widget_now_playing_badge_container",
    "03_widget_now_playing_timecode_container",
    "03_widget_now_playing_timecode_text",
    "02_widget_clock_text",
    "05_widget_audio_config_text",
    "05_widget_audio_config_volume_text",
)

_INDICATOR_LAYER_LOGICAL: tuple[tuple[str, str], ...] = (
    ("01_icon_audio_indicator", "indicator_audio"),
    ("01_icon_now_playing_indicator", "indicator_now_playing"),
    ("01_icon_indicator_reveiver", "indicator_receiver"),
    ("01_icon_indicator_tmdb", "indicator_tmdb"),
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


# Backdrop inset (layer 04).
_BACKDROP_X = _sx(21.522)
_BACKDROP_Y = _sy(61.469)
_BACKDROP_W = _sx(212.293)
_BACKDROP_H = _sy(108.307)
_BACKDROP_RX = max(2, _sx(11.108))

# Status bar group (layer 03).
_BAR_L = _sx(255.337)
_BAR_T = _sy(98.997)
_BAR_W = _sx(487.96)
_BAR_H = _sy(216.295)
_BAR_RX = max(4, _sx(23.455))
_BAR_STROKE = max(1, _sx(3.0))

# Badge + timecode containers (layer 03).
_BADGE_W = _sx(182.571)
_BADGE_H = _sy(39.63)
_TC_W = _sx(96.418)
_TC_H = _sy(39.63)
_BADGE_Y = _sy(60.492)
_TC_Y = _sy(314.493)

# Clock (layer 02) — right-aligned near x=503 in source art.
_CLOCK_X = _sx(503.5525)
_CLOCK_Y = _sy(452.3979)
_CLOCK_SIZE_PX = max(12, _sy(60.0))

# Audio config + volume (layer 05).
_AUDIO_CFG_X = _sx(31.4322)
_AUDIO_CFG_Y = _sy(215.9585)
_AUDIO_CFG_SIZE = max(10, _sy(26.0))
_VOLUME_X = _sx(64.672)
_VOLUME_Y = _sy(444.8463)
_VOLUME_SIZE = max(10, _sy(34.0))

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


def default_now_playing_svg_path(assets_dir: Path | str | None = None) -> Path:
    """Resolve now-playing SVG (override with ``PIGEON_NOW_PLAYING_SVG``)."""
    env = os.environ.get("PIGEON_NOW_PLAYING_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "now_playing_test_070126.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "now_playing_test_070126.svg"


def _find_by_id(root: ET.Element, layer_id: str) -> ET.Element | None:
    for el in root.iter():
        if el.get("id") == layer_id:
            return el
    return None


def _find_by_logical_id(root: ET.Element, logical_id: str) -> ET.Element | None:
    return _find_by_id(root, _encode_svg_layer_id(logical_id))


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


def apply_now_playing_svg_state(root: ET.Element, state: NowPlayingScreenState) -> None:
    """Mutate an SVG element tree: hide demo/dynamic layers; recolor status dots."""
    for logical_id in _HIDE_LAYER_LOGICAL:
        _set_visible(_find_by_logical_id(root, logical_id), False)
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
    except ImportError:
        pass

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
    except OSError as exc:
        raise RuntimeError(
            "Now-playing screen needs PyMuPDF (pip install pymupdf) or cairosvg with system cairo."
        ) from exc

    raise RuntimeError("Install pymupdf or cairosvg to rasterize the now-playing SVG.")


def render_now_playing_svg_base_bgra(
    state: NowPlayingScreenState,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    """Load the now-playing SVG, apply ``state`` to static chrome, return 800×400 BGRA."""
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
    try:
        return ImageFont.truetype(path, max(6, size))
    except OSError:
        return ImageFont.load_default()


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
    anchor: str = "ls",
) -> tuple[np.ndarray, int, int]:
    if not text:
        return np.zeros((1, 1, 4), dtype=np.uint8), 0, 0
    path = resolve_ui_font_extrabold() if bold else resolve_ui_font_medium()
    if not path:
        path = resolve_ui_font_bold()
    font = _load_font(str(path or ""), size_px)
    probe = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    tw, th = max(1, r - l), max(1, b - t)
    img = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        draw.text((-l + 2, -t + 2), text, font=font, fill=(*fill_rgb, 255), anchor=anchor)
    except (TypeError, ValueError):
        draw.text((-l + 2, -t + 2), text, font=font, fill=(*fill_rgb, 255))
    arr = np.asarray(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA), tw, th


def _clock_text(now: datetime | None = None) -> str:
    dt = now if now is not None else datetime.now()
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{dt.strftime('%M')}{dt.strftime('%p')}"


def _audio_config_line(incoming: str, config: str) -> str:
    inc = _receiver_audio_display_line(incoming)
    cfg = _receiver_audio_display_line(config)
    if inc and cfg:
        return f"{inc} > {cfg}"
    return inc or cfg


def _tt_to_black_bgra(src: np.ndarray) -> np.ndarray:
    if src is None or src.size == 0:
        return src
    out = src.copy()
    alpha = out[:, :, 3] > 0
    out[alpha, 0] = 0
    out[alpha, 1] = 0
    out[alpha, 2] = 0
    return out


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
        self._badge_bgra: np.ndarray | None = None
        self._cached_bgra: np.ndarray | None = None
        self._cached_sig: tuple[object, ...] | None = None

    @property
    def chrome_visible(self) -> bool:
        return self._state.chrome_visible

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
            s = str(val or "")
            if s != getattr(self._state, key):
                setattr(self._state, key, s)
                changed = True
        if changed:
            self.clear_cache()
        return changed

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
            now_playing=bool(has_now_playing),
            receiver=bool(has_receiver),
            tmdb=bool(has_tmdb),
            audio=bool(audio_analysis),
        ):
            changed = True
        if self.set_backdrop_bgr(tmdb_backdrop_bgr):
            changed = True
        if self.set_tt_bgra(tmdb_tt_bgra):
            changed = True
        badge_arr = (
            np.asarray(service_badge_bgra, dtype=np.uint8).copy()
            if service_badge_bgra is not None and service_badge_bgra.size > 0
            else None
        )
        badge_id = id(badge_arr) if badge_arr is not None else None
        prev_id = id(self._badge_bgra) if self._badge_bgra is not None else None
        if badge_id != prev_id:
            self._badge_bgra = badge_arr
            changed = True
        elif badge_arr is not None and self._badge_bgra is not None:
            if not np.array_equal(badge_arr, self._badge_bgra):
                self._badge_bgra = badge_arr
                changed = True
        elif badge_arr is None and self._badge_bgra is not None:
            self._badge_bgra = None
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
            bd_id,
            tt_id,
            id(self._badge_bgra) if self._badge_bgra is not None else None,
            int(datetime.now().strftime("%H%M")),  # clock minute bucket
        )

    def _paste_patch(self, canvas: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
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
            roi[:, :, :3] = alpha_blend_bgra_over_bgr(roi[:, :, :3], sub)
            roi[:, :, 3] = np.maximum(roi[:, :, 3], sub[:, :, 3])
        else:
            roi[:] = alpha_blend_bgra_over_bgr(roi, sub)

    def _render_svg_base(self) -> np.ndarray:
        try:
            return render_now_playing_svg_base_bgra(self._state, assets_dir=self._assets_dir)
        except (FileNotFoundError, RuntimeError):
            return _fallback_base_bgra()

    def _render_frame_bgra(self) -> np.ndarray:
        st = self._state
        out = self._render_svg_base()

        # Live TMDb backdrop inside SVG container bounds.
        if self._backdrop_bgr is not None and self._backdrop_bgr.size > 0:
            bd_patch = cv2.resize(
                self._backdrop_bgr,
                (_BACKDROP_W, _BACKDROP_H),
                interpolation=cv2.INTER_AREA,
            )
            if bd_patch.ndim == 2:
                bd_patch = cv2.cvtColor(bd_patch, cv2.COLOR_GRAY2BGR)
            if bd_patch.shape[2] == 4:
                bd_bgra = bd_patch
            else:
                bd_bgra = cv2.cvtColor(bd_patch, cv2.COLOR_BGR2BGRA)
            mask = _rounded_rect_mask(_BACKDROP_W, _BACKDROP_H, _BACKDROP_RX)
            bd_bgra = bd_bgra.copy()
            bd_bgra[:, :, 3] = cv2.bitwise_and(bd_bgra[:, :, 3], mask)
            self._paste_patch(out, bd_bgra, _BACKDROP_X, _BACKDROP_Y)

        progress = st.progress if st.trt_substantive else 0.0
        played_w = int(round(progress * float(_BAR_W)))
        played_w = max(0, min(_BAR_W, played_w))

        # Played bar fill (under title treatment).
        if played_w > 0:
            _draw_rounded_rect_bgra(
                out,
                _BAR_L,
                _BAR_T,
                played_w,
                _BAR_H,
                fill_bgr=_COLOR_UI_BGR,
                stroke_bgr=_COLOR_ACCENT_BGR,
                radius=_BAR_RX,
                stroke=_BAR_STROKE,
            )

        # TMDb TT: color in played region; black in unplayed (reveals color left→right).
        if self._tt_bgra is not None and self._tt_bgra.size > 0:
            tt_fit = _image_contain_center_bgra(self._tt_bgra, _BAR_W, _BAR_H)
            if played_w > 0:
                color_crop = tt_fit[:, :played_w, :].copy()
                self._paste_patch(out, color_crop, _BAR_L, _BAR_T)
            if played_w < _BAR_W:
                tt_black = _tt_to_black_bgra(tt_fit)
                crop = tt_black[:, played_w:, :].copy()
                self._paste_patch(out, crop, _BAR_L + played_w, _BAR_T)

        if st.show_paused and played_w > 0:
            paused = _text_patch_bgra(
                "paused",
                max(8, played_w),
                max(8, _BAR_H),
                align="center",
                fill_rgba=(255, 255, 255, 230),
            )
            self._paste_patch(out, paused, _BAR_L, _BAR_T)

        # Badge container + logo.
        badge_x = _follow_container_x(_BADGE_W, _BAR_L, _BAR_W, progress)
        _draw_rounded_rect_bgra(
            out,
            badge_x,
            _BADGE_Y,
            _BADGE_W,
            _BADGE_H,
            fill_bgr=_COLOR_ACCENT_BGR,
            radius=max(2, _sy(8)),
        )
        if self._badge_bgra is not None:
            badge_inner = _image_contain_center_bgra(self._badge_bgra, _BADGE_W - 8, _BADGE_H - 8)
            self._paste_patch(out, badge_inner, badge_x + 4, _BADGE_Y + 4)
        elif st.badge_show and str(st.badge_label or "").strip():
            badge_inner = _text_patch_bgra(
                str(st.badge_label),
                _BADGE_W,
                _BADGE_H,
                align="center",
                fill_rgba=(32, 32, 32, 255),
            )
            self._paste_patch(out, badge_inner, badge_x, _BADGE_Y)

        # Timecode container + remaining text.
        tc_x = _follow_container_x(_TC_W, _BAR_L, _BAR_W, progress)
        _draw_rounded_rect_bgra(
            out,
            tc_x,
            _TC_Y,
            _TC_W,
            _TC_H,
            fill_bgr=_COLOR_ACCENT_BGR,
            radius=max(2, _sy(8)),
        )
        tc_text = str(st.remaining_text or "").strip()
        if tc_text and st.trt_substantive:
            tc_patch, tw, th = _fit_text_patch(
                tc_text,
                size_px=max(10, _sy(30.0)),
                fill_rgb=(40, 40, 40),
                bold=False,
            )
            tx = tc_x + max(0, (_TC_W - tw) // 2)
            ty = _TC_Y + max(0, (_TC_H - th) // 2)
            self._paste_patch(out, tc_patch, tx, ty)

        # Clock — right aligned at design x anchor.
        clk = _clock_text()
        clk_patch, cw, ch = _fit_text_patch(
            clk,
            size_px=_CLOCK_SIZE_PX,
            fill_rgb=(225, 0, 24),
            bold=True,
            anchor="rs",
        )
        self._paste_patch(out, clk_patch, _CLOCK_X - cw, _CLOCK_Y - ch // 2)

        # Audio config + volume (placeholder SVG text hidden; live values drawn here).
        cfg_line = _audio_config_line(st.incoming, st.config)
        if cfg_line:
            cfg_patch, _, _ = _fit_text_patch(
                cfg_line,
                size_px=_AUDIO_CFG_SIZE,
                fill_rgb=(225, 0, 24),
                bold=True,
            )
            self._paste_patch(out, cfg_patch, _AUDIO_CFG_X, _AUDIO_CFG_Y - _sy(20))
        vol_line = _receiver_volume_display_line(st.volume)
        if vol_line:
            vol_patch, _, _ = _fit_text_patch(
                vol_line,
                size_px=_VOLUME_SIZE,
                fill_rgb=(225, 0, 24),
                bold=True,
            )
            self._paste_patch(out, vol_patch, _VOLUME_X, _VOLUME_Y - _sy(26))

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
        self._cached_bgra = self._render_frame_bgra()
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
