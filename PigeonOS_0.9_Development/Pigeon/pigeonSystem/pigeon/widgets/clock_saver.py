"""Idle clock saver: SVG clock + weather chrome with cycling color.

Layout from ``pigeonAssets/clocksaver.svg``:
  • ``tday_month_year_text`` / ``today_month_year_text`` — Sharp Sans Bold date
  • ``hhmmss_text`` — Digital-7 HH:MM:SS
  • ``high_temp`` / ``low_temp`` — daily °F for the configured ZIP
  • ``degrees_left_stroke`` / ``degrees_rifght_stroke`` — stroke-only ° marks
  • ``sun_fill`` / ``moon`` — day/night icons
"""

from __future__ import annotations

import copy
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.design import DESIGN_H, DESIGN_W, GRID_COLS, get_grid_geometry
from pigeon.font_paths import (
    resolve_digital7_font,
    resolve_ui_font_bold,
)
from pigeon.weather import DEFAULT_WEATHER_ZIP, ensure_weather
from pigeon.widgets.clock_calendar import _resolve_display_time

# Large saver time band (legacy callers); full-frame SVG is preferred now.
_CLOCK_SAVER_TIME_ROW_TOP_1BASED = 2
_CLOCK_SAVER_TIME_ROW_END_1BASED = 7

_TIME_FORMAT = "%H:%M:%S"
_DATE_FORMAT = "%A, %B %-d" if os.name != "nt" else "%A, %B %#d"

_TIME_COLOR_SEGMENT_S = 10.0
_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-\d.]+)(?:[,\s]+([-\d.]+))?\s*\)",
    re.IGNORECASE,
)

_SVG_TREE_TEMPLATES: dict[tuple[str, int], ET.Element] = {}
_EMPTY_PATCH = np.zeros((1, 1, 4), dtype=np.uint8)

# Artboard center for centered date / time labels (clocksaver.svg viewBox).
_ARTBOARD_CX = 805.89 * 0.5
_ARTBOARD_W = 805.89
_ARTBOARD_H = 481.0
# Illustrator baseline / size for ``hhmmss_text`` (fixed-cell redraw uses these).
_HHMMSS_BASELINE_Y_SVG = 273.79
_HHMMSS_FONT_SIZE_SVG = 231.0
_HHMMSS_CHAR_SET = "0123456789:"
# Slight horizontal inset so the 8-slot block stays inside the plate.
_HHMMSS_SIDE_PAD_PX = 28
# Max fraction of design width the fixed-cell block may occupy.
_HHMMSS_MAX_WIDTH_FRAC = 0.92


def _time_color_rgba(now_mono: float) -> tuple[int, int, int, int]:
    """Hold current color 10s, then 10s transition to the next; timeline starts on white."""
    import colorsys

    H = float(_TIME_COLOR_SEGMENT_S)
    t = max(0.0, float(now_mono))
    seg = int(t // H)
    u = (t % H) / H if H > 0 else 0.0

    def _rgb_block(k: int) -> tuple[float, float, float]:
        if k <= 0:
            return (1.0, 1.0, 1.0)
        hue = ((k - 1) * 0.3021688479) % 1.0
        return colorsys.hsv_to_rgb(hue, 0.72, 0.96)

    if seg % 2 == 0:
        r, g, b = _rgb_block(seg // 2)
        return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), 255)
    a = _rgb_block(seg // 2)
    b = _rgb_block(seg // 2 + 1)
    r = a[0] + (b[0] - a[0]) * u
    g = a[1] + (b[1] - a[1]) * u
    bl = a[2] + (b[2] - a[2]) * u
    return (int(round(r * 255)), int(round(g * 255)), int(round(bl * 255)), 255)


def _rgba_to_hex(rgba: tuple[int, int, int, int]) -> str:
    return f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}"


def default_clock_saver_svg_path(assets_dir: Path | str | None = None) -> Path:
    env = os.environ.get("PIGEON_CLOCK_SAVER_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "clocksaver.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "clocksaver.svg"


def _normalize_logical(raw_id: str) -> str:
    if not raw_id:
        return ""
    s = str(raw_id)
    for a, b in (
        ("_x5F_", "_"),
        ("_x2D_", "-"),
        ("%20", " "),
    ):
        s = s.replace(a, b)
    return s.strip().lower()


def _find_by_logical_id(root: ET.Element, *logical_ids: str) -> ET.Element | None:
    want = {_normalize_logical(x) for x in logical_ids if x}
    for el in root.iter():
        lid = _normalize_logical(el.get("id") or "")
        if lid in want:
            return el
        dname = _normalize_logical(el.get("data-name") or "")
        if dname in want:
            return el
    return None


def _set_flat_text(text_el: ET.Element | None, value: str) -> None:
    if text_el is None:
        return
    for child in list(text_el):
        text_el.remove(child)
    text_el.text = value


def _set_translate(el: ET.Element, x: float, y: float) -> None:
    el.set("transform", f"translate({x:.2f} {y:.2f})")


def _parse_translate_y(el: ET.Element) -> float:
    transform = el.get("transform") or ""
    match = _TRANSLATE_RE.search(transform)
    if match:
        return float(match.group(2) or 0.0)
    return 0.0


def _set_visible(el: ET.Element | None, visible: bool) -> None:
    if el is None:
        return
    if visible:
        el.attrib.pop("display", None)
        style = el.get("style") or ""
        style = re.sub(r"display\s*:\s*none;?", "", style, flags=re.I).strip().strip(";")
        if style:
            el.set("style", style)
        elif "style" in el.attrib:
            el.attrib.pop("style")
    else:
        el.set("display", "none")


def _paint_cycle_color(root: ET.Element, color_hex: str) -> None:
    """Retint fills/strokes to the cycle color; keep knock-out blacks and empty fills."""
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if tag in ("svg", "g", "defs", "clippath", "mask"):
            continue
        lid = _normalize_logical(el.get("id") or "")
        if lid in ("layer_1",) or lid.startswith("layer_"):
            continue
        fill = (el.get("fill") or "").strip().lower()
        stroke = (el.get("stroke") or "").strip().lower()
        # Background plate stays black / hidden separately.
        if tag == "rect" and fill in ("#040603", "#000", "#000000"):
            continue
        # Crescent cutout stays opaque black so the moon reads on any cycle color.
        if fill in ("#000", "#000000", "black") and stroke in ("", "none"):
            continue
        if fill and fill not in ("none", "transparent"):
            el.set("fill", color_hex)
        if stroke and stroke not in ("none", "transparent"):
            el.set("stroke", color_hex)
        if tag == "text":
            el.set("fill", color_hex)


def _ensure_degree_strokes(root: ET.Element, color_hex: str) -> None:
    """Force ° ellipses to stroke-only (no fill), matching the art direction."""
    for lid in (
        "degrees_left_stroke",
        "degrees_right_stroke",
        "degrees_rifght_stroke",  # Illustrator typo in export
    ):
        el = _find_by_logical_id(root, lid)
        if el is None:
            continue
        el.set("fill", "none")
        el.set("stroke", color_hex)
        if not (el.get("stroke-width") or "").strip():
            el.set("stroke-width", "3")


def _date_label(now) -> str:
    # Prefer "Monday, August 18" (no leading zero on day).
    try:
        return now.strftime(_DATE_FORMAT)
    except ValueError:
        return now.strftime("%A, %B %d").replace(" 0", " ")


def _svg_tree_from_path(path: Path) -> ET.Element:
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    template = _SVG_TREE_TEMPLATES.get(key)
    if template is None:
        tree = ET.parse(path)
        root = tree.getroot()
        # Fit Illustrator artboard to design canvas.
        root.set("viewBox", "0 0 805.89 481")
        root.set("width", str(DESIGN_W))
        root.set("height", str(DESIGN_H))
        _SVG_TREE_TEMPLATES.clear()
        _SVG_TREE_TEMPLATES[key] = root
        template = root
    return copy.deepcopy(template)


def _apply_clock_saver_svg_state(root: ET.Element, *, color_hex: str) -> None:
    now = _resolve_display_time()
    date_el = _find_by_logical_id(
        root, "today_month_year_text", "tday_month_year_text"
    )
    time_el = _find_by_logical_id(root, "hhmmss_text")
    high_el = _find_by_logical_id(root, "high_temp")
    low_el = _find_by_logical_id(root, "low_temp")

    date_text = _date_label(now)
    temps = ensure_weather(zip_code=DEFAULT_WEATHER_ZIP)
    high_s = f"{temps.high_f}" if temps is not None else "--"
    low_s = f"{temps.low_f}" if temps is not None else "--"

    if date_el is not None:
        _set_flat_text(date_el, date_text)
        _set_translate(date_el, _ARTBOARD_CX, _parse_translate_y(date_el) or 91.98)
        date_el.set("text-anchor", "middle")
        date_el.set("font-family", "SharpSans-Bold, 'Sharp Sans'")
        date_el.set("font-weight", "700")
    if time_el is not None:
        # Drawn in Pillow with fixed-width cells (Digital-7 glyphs are not tabular).
        _set_visible(time_el, False)
    if high_el is not None:
        _set_flat_text(high_el, high_s)
    if low_el is not None:
        _set_flat_text(low_el, low_s)

    # Hide baked black plate — callers already paint a black stage.
    _set_visible(_find_by_logical_id(root, "Layer_1", "layer_1"), False)
    for el in list(root):
        if el.tag.endswith("rect"):
            fill = (el.get("fill") or "").strip().lower()
            if fill in ("#040603", "#000", "#000000"):
                _set_visible(el, False)

    _paint_cycle_color(root, color_hex)
    _ensure_degree_strokes(root, color_hex)


def _apply_layer_opacity(bgra: np.ndarray, op: float) -> np.ndarray:
    o = max(0.0, min(1.0, float(op)))
    if o >= 0.999:
        return bgra
    out = bgra.astype(np.float32)
    out[:, :, 3] *= o
    return np.clip(out, 0, 255).astype(np.uint8)


def _load_font(path: str | None, size: int) -> ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _char_bbox(
    draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, ch: str
) -> tuple[int, int, int, int]:
    return draw.textbbox((0, 0), ch, font=font)


def _cell_metrics(
    draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, char_set: str
) -> tuple[int, int]:
    max_w = 1
    max_h = 1
    for ch in char_set:
        l, t, r, b = _char_bbox(draw, font, ch)
        max_w = max(max_w, r - l)
        max_h = max(max_h, b - t)
    return max_w, max_h


def _fit_digital7_fixed_cells(
    path: str | None,
    num_slots: int,
    *,
    max_w: int,
    max_h: int,
    prefer_sz: int,
    min_sz: int = 12,
) -> ImageFont.ImageFont:
    """Largest Digital-7 size that keeps ``num_slots`` equal cells inside the box."""
    if max_w < 4 or max_h < 4 or num_slots < 1:
        return _load_font(path, min_sz)
    lo, hi = min_sz, max(prefer_sz, max_h, 24)
    best = _load_font(path, min_sz)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _load_font(path, mid)
        cw, ch = _cell_metrics(probe, f, _HHMMSS_CHAR_SET)
        if num_slots * cw <= max_w and ch <= max_h:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _draw_hhmmss_fixed_cells(
    bgra: np.ndarray,
    time_text: str,
    *,
    color: tuple[int, int, int, int],
) -> None:
    """Paint ``HH:MM:SS`` with equal horizontal slots so Digital-7 never shifts."""
    s = str(time_text or "").strip() or "00:00:00"
    n = max(1, len(s))
    font_path = resolve_digital7_font() or resolve_ui_font_bold()
    sx = float(DESIGN_W) / _ARTBOARD_W
    sy = float(DESIGN_H) / _ARTBOARD_H
    prefer_sz = max(24, int(round(_HHMMSS_FONT_SIZE_SVG * sy)))
    baseline_y = int(round(_HHMMSS_BASELINE_Y_SVG * sy))
    max_w = max(
        64,
        int(round(DESIGN_W * _HHMMSS_MAX_WIDTH_FRAC)) - 2 * _HHMMSS_SIDE_PAD_PX,
    )
    max_h = max(24, int(round(prefer_sz * 1.15)))
    font = _fit_digital7_fixed_cells(
        font_path,
        n,
        max_w=max_w,
        max_h=max_h,
        prefer_sz=prefer_sz,
    )
    rgba = np.ascontiguousarray(bgra[:, :, [2, 1, 0, 3]])
    img = Image.fromarray(rgba)
    draw = ImageDraw.Draw(img)
    cell_w, cell_h = _cell_metrics(draw, font, _HHMMSS_CHAR_SET)
    block_w = n * cell_w
    block_x = (DESIGN_W - block_w) // 2
    # SVG text used a baseline; center glyphs on a mid-line near that baseline.
    cy = baseline_y - max(1, cell_h // 5)
    for i, glyph in enumerate(s):
        cx = block_x + i * cell_w + cell_w // 2
        draw.text((cx, cy), glyph, font=font, fill=color, anchor="mm")
    out = np.asarray(img)
    bgra[:, :, 0] = out[:, :, 2]
    bgra[:, :, 1] = out[:, :, 1]
    bgra[:, :, 2] = out[:, :, 0]
    bgra[:, :, 3] = out[:, :, 3]


def clock_saver_time_design_rect() -> tuple[int, int, int, int]:
    """Pixel rect for large saver time: full grid width, rows 2 .. top of row 7."""
    g = get_grid_geometry()
    cell = g.cell
    x = g.x0
    w = GRID_COLS * cell
    r_top = int(_CLOCK_SAVER_TIME_ROW_TOP_1BASED)
    r_end = int(_CLOCK_SAVER_TIME_ROW_END_1BASED)
    r_top = max(1, r_top)
    r_end = max(r_top + 1, r_end)
    y = g.y0 + (r_top - 1) * cell
    h_time = max(1, (r_end - r_top) * cell)
    return (x, y, w, h_time)


def render_clock_saver_bgra(
    *,
    layer_opacity: float = 1.0,
    assets_dir: Path | str | None = None,
    svg_path: Path | str | None = None,
) -> np.ndarray:
    """Full 800×480 BGRA clock saver frame (transparent outside chrome)."""
    path = (
        Path(svg_path)
        if svg_path is not None
        else default_clock_saver_svg_path(assets_dir)
    )
    color = _time_color_rgba(time.monotonic())
    color_hex = _rgba_to_hex(color)
    time_text = _resolve_display_time().strftime(_TIME_FORMAT)
    if not path.is_file():
        # Soft fallback: time-only band if art is missing.
        return _legacy_time_only_bgra(
            color=color, layer_opacity=layer_opacity, time_text=time_text
        )

    root = _svg_tree_from_path(path)
    _apply_clock_saver_svg_state(root, color_hex=color_hex)
    from pigeon.widgets.settings_svg_text import rasterize_settings_svg_bgra

    bgra = rasterize_settings_svg_bgra(
        root,
        width=DESIGN_W,
        height=DESIGN_H,
        font_mode="preferences",
    )
    _draw_hhmmss_fixed_cells(bgra, time_text, color=color)
    return _apply_layer_opacity(bgra, layer_opacity)


def _legacy_time_only_bgra(
    *,
    color: tuple[int, int, int, int],
    layer_opacity: float,
    time_text: str | None = None,
) -> np.ndarray:
    bgra = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
    text = time_text or _resolve_display_time().strftime(_TIME_FORMAT)
    _draw_hhmmss_fixed_cells(bgra, text, color=color)
    return _apply_layer_opacity(bgra, layer_opacity)


def clock_saver_composite_bgra(
    *,
    shadow_bgr: tuple[int, int, int] | None,
    layer_opacity: float = 1.0,
    time_layer_opacity: float | None = None,
    date_layer_opacity: float | None = None,
    date_anchor_row: int | None = None,
    date_anchor_col: int | None = None,
) -> tuple[
    tuple[np.ndarray, tuple[int, int, int, int]],
    tuple[np.ndarray, tuple[int, int, int, int]],
]:
    """
    Full-frame clock+weather saver.

    Returns ``(full_frame, empty_date)`` so existing compose call sites that
    blit both packs keep working. ``shadow_bgr`` / date anchors are unused
    (SVG art is flat color-cycled chrome).
    """
    _ = (shadow_bgr, date_layer_opacity, date_anchor_row, date_anchor_col)
    t_op = float(layer_opacity if time_layer_opacity is None else time_layer_opacity)
    frame = render_clock_saver_bgra(layer_opacity=t_op)
    full_rect = (0, 0, int(DESIGN_W), int(DESIGN_H))
    return (frame, full_rect), (_EMPTY_PATCH.copy(), (0, 0, 1, 1))


def clear_clock_saver_render_caches() -> None:
    _SVG_TREE_TEMPLATES.clear()
