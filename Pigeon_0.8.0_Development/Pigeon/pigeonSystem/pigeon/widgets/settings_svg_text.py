"""
Pillow text overlay for settings / keyboard SVGs.

PyMuPDF rasterizes SVG paths but substitutes wrong fonts for ``<text>``.
We strip SVG text, rasterize shapes, then redraw labels with Pillow.

Font rule (``settingInstructions_0.8.0``):
  • Layers under ``main_instructions`` → Sharp Sans (semibold)
  • All keyboard SVG text → Sharp Sans (semibold)
  • Everything else → Digital-7 Regular
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Callable, Literal

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.font_paths import (
    resolve_digital7_font,
    resolve_ui_font_semibold,
)

_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-\d.]+)(?:[,\s]+([-\d.]+))?\s*\)",
    re.IGNORECASE,
)
_MATRIX_RE = re.compile(
    r"matrix\(\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)",
    re.IGNORECASE,
)


class SettingsFontRole(str, Enum):
    DIGITAL7 = "digital7"
    SHARP_SEMIBOLD = "semibold"


_INSTRUCTION_GROUP_IDS = frozenset({"main_instructions"})
_KEYBOARD_GROUP_IDS = frozenset({"keyboardtemp"})
_AI_SUFFIX_RE = re.compile(r"_\d{20,}_?$")

SettingsTextFontMode = Literal["auto", "keyboard"]


@dataclass(frozen=True)
class SettingsTextDrawOp:
    x_px: int
    y_px: int
    text: str
    size_px: int
    fill_rgba: tuple[int, int, int, int]
    role: SettingsFontRole
    anchor: str = "ls"


def viewbox_from_root(root: ET.Element) -> tuple[float, float, float, float]:
    raw = (root.get("viewBox") or "").strip()
    if raw:
        parts = [float(p) for p in raw.replace(",", " ").split()]
        if len(parts) == 4:
            return parts[0], parts[1], parts[2], parts[3]
    w = float(root.get("width") or 800)
    h = float(root.get("height") or 400)
    return 0.0, 0.0, w, h


def _style_prop(style: str | None, prop: str) -> str | None:
    if not style:
        return None
    m = re.search(rf"{re.escape(prop)}\s*:\s*([^;\"']+)", style, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip("'\"")


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#").lower()
    if len(h) == 3:
        h = f"{h[0]}{h[0]}{h[1]}{h[1]}{h[2]}{h[2]}"
    if len(h) != 6:
        return (255, 255, 255, 255)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def _parse_fill(el: ET.Element) -> str:
    fill = (el.get("fill") or "").strip()
    if not fill or fill.lower() in ("none", "transparent"):
        fill = _style_prop(el.get("style"), "fill") or ""
    if not fill or fill.lower() in ("none", "transparent"):
        return "#ffffff"
    if fill.startswith("#"):
        return fill.lower()
    named = fill.lower()
    if named == "white":
        return "#ffffff"
    if named == "black":
        return "#000000"
    return fill


def _parse_font_size(el: ET.Element, *, vb_h: float, out_h: int) -> int:
    raw = el.get("font-size") or _style_prop(el.get("style"), "font-size") or "14"
    raw = raw.replace("px", "").strip()
    try:
        size_svg = float(raw)
    except ValueError:
        size_svg = 14.0
    return max(6, int(round(size_svg * out_h / max(vb_h, 1.0))))


def _parse_text_anchor(el: ET.Element) -> str:
    raw = (el.get("text-anchor") or _style_prop(el.get("style"), "text-anchor") or "start").lower()
    baseline = (
        el.get("dominant-baseline")
        or _style_prop(el.get("style"), "dominant-baseline")
        or ""
    ).lower()
    if raw == "middle" and baseline == "middle":
        return "mm"
    return {"middle": "ms", "end": "rs", "start": "ls"}.get(raw, "ls")


def _normalize_logical(raw_id: str) -> str:
    """Decode Illustrator id encoding and drop uniqueness suffixes."""
    if not raw_id:
        return ""
    out: list[str] = []
    i = 0
    n = len(raw_id)
    while i < n:
        if raw_id.startswith("_x5F_", i):
            out.append("_")
            i += 5
            continue
        if i + 5 <= n and raw_id[i] == "_" and raw_id[i + 1] == "x" and raw_id[i + 4] == "_":
            try:
                out.append(chr(int(raw_id[i + 2 : i + 4], 16)))
                i += 5
                continue
            except ValueError:
                pass
        out.append(raw_id[i])
        i += 1
    return _AI_SUFFIX_RE.sub("", "".join(out))


def _is_instruction_text(
    text_el: ET.Element,
    parents: dict[ET.Element, ET.Element],
) -> bool:
    """True when the text layer lives under ``main_instructions`` (or similar)."""
    cur: ET.Element | None = text_el
    while cur is not None:
        logical = _normalize_logical(cur.get("id") or "")
        if logical in _INSTRUCTION_GROUP_IDS or logical.endswith("_instructions"):
            return True
        if "instruction" in logical and logical.endswith("_text"):
            return True
        cur = parents.get(cur)
    return False


def _is_keyboard_text(
    text_el: ET.Element,
    parents: dict[ET.Element, ET.Element],
) -> bool:
    """True for keyboard overlays (``keyboardtemp`` or ``keyboard_*`` / ``symbolic_*`` ids)."""
    cur: ET.Element | None = text_el
    while cur is not None:
        logical = _normalize_logical(cur.get("id") or "")
        if logical in _KEYBOARD_GROUP_IDS:
            return True
        if logical.startswith("keyboard") or logical.startswith("symbolic_"):
            return True
        cur = parents.get(cur)
    logical = _normalize_logical(text_el.get("id") or "")
    return logical.startswith("keyboard") or logical.startswith("symbolic_")


def _font_role_for_text(
    text_el: ET.Element,
    parents: dict[ET.Element, ET.Element],
    *,
    font_mode: SettingsTextFontMode = "auto",
) -> SettingsFontRole:
    if font_mode == "keyboard" or _is_instruction_text(text_el, parents) or _is_keyboard_text(
        text_el, parents
    ):
        return SettingsFontRole.SHARP_SEMIBOLD
    return SettingsFontRole.DIGITAL7


def _parse_text_xy(el: ET.Element) -> tuple[float, float] | None:
    transform = el.get("transform") or ""
    m = _MATRIX_RE.search(transform)
    if m:
        return float(m.group(5)), float(m.group(6))
    t = _TRANSLATE_RE.search(transform)
    if t:
        return float(t.group(1)), float(t.group(2) or 0.0)
    return None


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent
    return parents


def _is_hidden(el: ET.Element, parents: dict[ET.Element, ET.Element]) -> bool:
    cur: ET.Element | None = el
    while cur is not None:
        if cur.get("display") == "none":
            return True
        style = cur.get("style") or ""
        if re.search(r"display\s*:\s*none", style, re.IGNORECASE):
            return True
        cur = parents.get(cur)
    return False


def _text_content(text_el: ET.Element) -> str:
    return "".join(text_el.itertext()).strip()


def collect_settings_text_ops(
    root: ET.Element,
    *,
    out_w: int,
    out_h: int,
    view_box: tuple[float, float, float, float] | None = None,
    font_mode: SettingsTextFontMode = "auto",
) -> list[SettingsTextDrawOp]:
    """Collect visible ``<text>`` nodes mapped to output pixel coordinates."""
    vb_x, vb_y, vb_w, vb_h = view_box if view_box is not None else viewbox_from_root(root)
    parents = _parent_map(root)
    ops: list[SettingsTextDrawOp] = []

    for text_el in root.iter():
        if not text_el.tag.endswith("text"):
            continue
        if _is_hidden(text_el, parents):
            continue
        pos = _parse_text_xy(text_el)
        if pos is None:
            continue
        content = _text_content(text_el)
        if not content:
            continue
        x_svg, y_svg = pos
        x_px = int(round((x_svg - vb_x) * out_w / max(vb_w, 1.0)))
        y_px = int(round((y_svg - vb_y) * out_h / max(vb_h, 1.0)))
        fill = _parse_fill(text_el)
        role = _font_role_for_text(text_el, parents, font_mode=font_mode)
        ops.append(
            SettingsTextDrawOp(
                x_px=x_px,
                y_px=y_px,
                text=content,
                size_px=_parse_font_size(text_el, vb_h=vb_h, out_h=out_h),
                fill_rgba=_hex_to_rgba(fill if fill.startswith("#") else "#ffffff"),
                role=role,
                anchor=_parse_text_anchor(text_el),
            )
        )
    return ops


def remove_svg_text(root: ET.Element) -> None:
    parents = _parent_map(root)
    for el in list(root.iter()):
        if el.tag.endswith("text"):
            parent = parents.get(el)
            if parent is not None:
                parent.remove(el)


@lru_cache(maxsize=16)
def _load_font(path: str, size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, max(6, size_px))
    except OSError:
        return ImageFont.load_default()


def _font_path_for_role(role: SettingsFontRole) -> str | None:
    if role == SettingsFontRole.DIGITAL7:
        return resolve_digital7_font()
    return resolve_ui_font_semibold()


def draw_settings_text_ops_bgra(bgra: np.ndarray, ops: list[SettingsTextDrawOp]) -> None:
    """Paint collected ops onto a BGRA frame in place."""
    if not ops:
        return

    # Prefer role fonts; fall back so missing Digital-7 never blanks the whole UI.
    fallback = resolve_ui_font_semibold() or resolve_digital7_font()
    if not any(_font_path_for_role(op.role) or fallback for op in ops):
        return

    rgb = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)

    for op in ops:
        path = _font_path_for_role(op.role) or fallback
        if not path:
            continue
        font = _load_font(path, op.size_px)
        draw.text((op.x_px, op.y_px), op.text, font=font, fill=op.fill_rgba, anchor=op.anchor)

    bgra[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA)


def _pymupdf_rasterize(root: ET.Element, *, width: int, height: int) -> np.ndarray:
    svg_bytes = ET.tostring(root, encoding="utf-8")
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=svg_bytes, filetype="svg")
        page = doc[0]
        pix = page.get_pixmap(
            matrix=fitz.Matrix(width / page.rect.width, height / page.rect.height),
            alpha=True,
        )
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGBA2BGRA)
        else:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGRA)
        if bgra.shape[0] != height or bgra.shape[1] != width:
            bgra = cv2.resize(bgra, (width, height), interpolation=cv2.INTER_AREA)
        return bgra
    except ImportError as exc:
        raise RuntimeError("Settings SVG rasterize needs PyMuPDF (pip install pymupdf).") from exc


def rasterize_settings_svg_bgra(
    root: ET.Element,
    *,
    width: int,
    height: int,
    view_box: tuple[float, float, float, float] | None = None,
    rasterize_fn: Callable[[ET.Element], np.ndarray] | None = None,
    font_mode: SettingsTextFontMode = "auto",
) -> np.ndarray:
    """
    Rasterize ``root`` with correct Digital-7 / Sharp Sans labels.

    Mutates ``root`` by removing ``<text>`` nodes before rasterizing shapes.
    """
    vb = view_box if view_box is not None else viewbox_from_root(root)
    ops = collect_settings_text_ops(
        root, out_w=width, out_h=height, view_box=vb, font_mode=font_mode
    )
    remove_svg_text(root)
    if rasterize_fn is not None:
        bgra = rasterize_fn(root)
    else:
        bgra = _pymupdf_rasterize(root, width=width, height=height)
    if bgra.shape[0] != height or bgra.shape[1] != width:
        bgra = cv2.resize(bgra, (width, height), interpolation=cv2.INTER_AREA)
    draw_settings_text_ops_bgra(bgra, ops)
    return bgra


__all__ = [
    "SettingsFontRole",
    "SettingsTextDrawOp",
    "collect_settings_text_ops",
    "draw_settings_text_ops_bgra",
    "rasterize_settings_svg_bgra",
    "remove_svg_text",
    "viewbox_from_root",
]
