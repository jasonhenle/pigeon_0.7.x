"""
Pigeon 0.7 settings page — SVG layer state and rasterization.

Loads ``settings_0.7.svg``, applies selection / variant visibility, and renders
to an 800×400 BGRA frame for preview or eventual in-app use.
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

COLOR_GREEN = "#02e900"
COLOR_BLACK = "#202020"

DESIGN_W = 800
DESIGN_H = 400
_VIEWBOX = "0 52.02 800 400.7"
_VIEWBOX_Y0 = 52.02
_VIEWBOX_H = 400.7

_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-\d.]+)(?:[,\s]+([-\d.]+))?\s*\)",
    re.IGNORECASE,
)

# SVG layer ids (deviceTwo button id has a typo in the source art).
_BUTTON_LAYER_IDS: tuple[str, ...] = (
    "selection_location",
    "selection_network",
    "button_deviceOne",
    "button_devbiceTwo",
    "button_deviceThree",
    "button_EXIT",
)

_WIFI_ICON_IDS: tuple[str, ...] = (
    "icon_WIFI_0",
    "icon_WIFI_1",
    "icon_WIFI_2",
    "icon_WIFI_3",
)

# WiFi fan clip from settings_0.7.svg (PyMuPDF does not honor SVG clip-path here).
_WIFI_CLIP_POLYGON_SVG: tuple[tuple[float, float], ...] = (
    (504.84, 129.03),
    (522.55, 128.89),
    (513.58, 144.16),
    (504.84, 159.56),
    (496.11, 144.16),
    (487.14, 128.89),
)
_WIFI_CX_SVG = 504.84
_WIFI_CY_SVG = 173.75
_WIFI_RADII_SVG: tuple[float, float, float] = (18.67, 29.58, 39.14)
_WIFI_STROKE_SVG = 5.0
_WIFI_SLASH_SVG = ((489.85, 155.55), (523.31, 132.06))
_WIFI_SLASH_STROKE_SVG = 3.0
_COLOR_GRAY_BGR = (128, 128, 128)

# Focus ring order: location → network → device 1 → device 2 → device 3 → exit → …
_NAVIGATION_LABELS: tuple[str, ...] = (
    "Location",
    "Network",
    "Device 1",
    "Device 2",
    "Device 3",
    "Exit",
)

_DEVICE_VARIANT_LAYERS: dict[str, dict[str, tuple[str, ...]]] = {
    "deviceOne": {
        "01": ("text_deviceOne_PIGEON", "text_deviceOne_IP", "text_deviceOne_URL"),
        "02": ("text_deviceOne__",),
    },
    "deviceTwo": {
        "01": ("text_deviceTwo_APPLETV", "text_deviceTwo_IP", "text_deviceTwo_player"),
        "02": ("text_deviceTwo__",),
    },
    "deviceThree": {
        "01": ("text_deviceThree_APPLETV", "text_deviceThree_IP", "text_deviceThree_reciever"),
        "02": ("text_deviceThree__",),
    },
}

_ALL_DEVICE_LAYER_IDS: frozenset[str] = frozenset(
    layer_id
    for device_layers in _DEVICE_VARIANT_LAYERS.values()
    for variant_layers in device_layers.values()
    for layer_id in variant_layers
)

_BUTTON_ASSOCIATED_TEXT: dict[str, tuple[str, ...]] = {
    "selection_location": ("text_location",),
    "selection_network": ("text_network", "icon_lock", *_WIFI_ICON_IDS),
    "button_deviceOne": (
        "text_deviceOne_PIGEON",
        "text_deviceOne_IP",
        "text_deviceOne_URL",
        "text_deviceOne__",
    ),
    "button_devbiceTwo": (
        "text_deviceTwo_APPLETV",
        "text_deviceTwo_IP",
        "text_deviceTwo_player",
        "text_deviceTwo__",
    ),
    "button_deviceThree": (
        "text_deviceThree_APPLETV",
        "text_deviceThree_IP",
        "text_deviceThree_reciever",
        "text_deviceThree__",
    ),
    "button_EXIT": ("text_EXIT",),
}


class SettingsButton(IntEnum):
    LOCATION = 0
    NETWORK = 1
    DEVICE_ONE = 2
    DEVICE_TWO = 3
    DEVICE_THREE = 4
    EXIT = 5


@dataclass
class SettingsPageState:
    """Interactive state for the settings menu preview."""

    selected: SettingsButton = SettingsButton.LOCATION
    device_one_variant: str = "01"
    device_two_variant: str = "01"
    device_three_variant: str = "02"
    wifi_level: int = 3
    network_locked: bool = True

    def advance(self, *, forward: bool = True) -> None:
        """Advance the focus ring (forward = next: location → … → exit → location)."""
        n = len(_BUTTON_LAYER_IDS)
        idx = int(self.selected)
        self.selected = SettingsButton((idx + (1 if forward else -1)) % n)

    @property
    def navigation_label(self) -> str:
        return _NAVIGATION_LABELS[int(self.selected)]


@dataclass(frozen=True)
class _TextDrawOp:
    x_svg: float
    y_svg: float
    text: str
    size_svg: float
    fill: str
    sharp_semibold: bool


def _resolve_settings_digital7_font() -> Path | None:
    """Digital-7 Regular for settings labels (non-italic)."""
    env = os.environ.get("PIGEON_FONT_CLOCK_SAVER", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    roots = (
        Path.home() / "Library/Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    )
    exact = ("digital-7.ttf", "Digital-7.ttf")
    for root in roots:
        if not root.is_dir():
            continue
        for name in exact:
            p = root / name
            if p.is_file():
                return p
    return None


def _resolve_settings_sharp_semibold_font() -> Path | None:
    env = os.environ.get("PIGEON_FONT_SEMIBOLD", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    roots = (
        Path.home() / "Library/Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    )
    globs = (
        "*Sharp*Sans*Semibold*.otf",
        "*Sharp*Sans*Semibold*.ttf",
        "*SharpSans*Semibold*.otf",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in globs:
            for p in sorted(root.glob(pattern)):
                if p.is_file() and "italic" not in p.name.lower():
                    return p
    return None


@lru_cache(maxsize=32)
def _load_pil_font(path: str, size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, max(6, size_px))
    except OSError:
        return ImageFont.load_default()


def _hex_to_rgba(hex_color: str) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#").lower()
    if len(h) != 6:
        return (0, 233, 2, 255)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)


def _svg_to_image_xy(x_svg: float, y_svg: float) -> tuple[int, int]:
    x = int(round(x_svg * DESIGN_W / 800.0))
    y = int(round((y_svg - _VIEWBOX_Y0) * DESIGN_H / _VIEWBOX_H))
    return x, y


def _svg_font_size_to_px(size_svg: float) -> int:
    return max(6, int(round(size_svg * DESIGN_H / _VIEWBOX_H)))


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
        cur = parents.get(cur)
    return False


def _parse_translate(transform: str | None) -> tuple[float, float] | None:
    if not transform:
        return None
    match = _TRANSLATE_RE.search(transform)
    if not match:
        return None
    x = float(match.group(1))
    y = float(match.group(2) or 0.0)
    return x, y


def _text_element_content(text_el: ET.Element) -> str:
    return "".join(text_el.itertext()).strip()


def _collect_text_draw_ops(root: ET.Element) -> list[_TextDrawOp]:
    parents = _parent_map(root)
    ops: list[_TextDrawOp] = []
    for text_el in root.iter():
        if not text_el.tag.endswith("text"):
            continue
        if _is_hidden(text_el, parents):
            continue
        pos = _parse_translate(text_el.get("transform"))
        if pos is None:
            continue
        content = _text_element_content(text_el)
        if not content:
            continue
        fill = (text_el.get("fill") or COLOR_GREEN).lower()
        if fill not in (COLOR_GREEN, COLOR_BLACK):
            continue
        try:
            size_svg = float(text_el.get("font-size", "14"))
        except ValueError:
            size_svg = 14.0
        family = (text_el.get("font-family") or "").lower()
        ops.append(
            _TextDrawOp(
                x_svg=pos[0],
                y_svg=pos[1],
                text=content,
                size_svg=size_svg,
                fill=fill,
                sharp_semibold="sharp" in family,
            )
        )
    return ops


def _svg_scale(value: float) -> float:
    return value * DESIGN_H / _VIEWBOX_H


def _svg_radius_to_px(radius_svg: float) -> int:
    return max(1, int(round(_svg_scale(radius_svg))))


def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    r, g, b, _ = _hex_to_rgba(hex_color)
    return b, g, r


def _remove_svg_wifi_icons(root: ET.Element) -> None:
    """Drop WiFi SVG groups (drawn separately with the AI clip mask)."""
    parents = _parent_map(root)
    to_remove = [el for el in root.iter() if (el.get("id") or "") in _WIFI_ICON_IDS]
    for el in to_remove:
        parent = parents.get(el)
        if parent is not None:
            parent.remove(el)


def _composite_stroke_mask(
    dst: np.ndarray,
    mask: np.ndarray,
    color_bgr: tuple[int, int, int],
) -> None:
    """Composite an anti-aliased stroke mask over ``dst`` using a flat icon color."""
    alpha = mask.astype(np.float32) / 255.0
    if not np.any(alpha > 0):
        return
    alpha3 = alpha[..., np.newaxis]
    background = dst[:, :, :3].astype(np.float32)
    foreground = np.array(color_bgr, dtype=np.float32)
    dst[:, :, :3] = np.clip(foreground * alpha3 + background * (1.0 - alpha3), 0, 255).astype(
        np.uint8
    )
    dst[:, :, 3] = 255


def _draw_wifi_icon_overlay(bgra: np.ndarray, state: SettingsPageState) -> None:
    """Draw the clipped WiFi strength icon (matches Illustrator clip-path)."""
    level = max(0, min(3, int(state.wifi_level)))
    network_selected = int(state.selected) == int(SettingsButton.NETWORK)
    active_bgr = _hex_to_bgr(COLOR_BLACK if network_selected else COLOR_GREEN)

    cx, cy = _svg_to_image_xy(_WIFI_CX_SVG, _WIFI_CY_SVG)
    radii = [_svg_radius_to_px(r) for r in _WIFI_RADII_SVG]
    stroke = max(1, int(round(_svg_scale(_WIFI_STROKE_SVG))))
    slash_stroke = max(1, int(round(_svg_scale(_WIFI_SLASH_STROKE_SVG))))

    clip_pts = np.array([_svg_to_image_xy(x, y) for x, y in _WIFI_CLIP_POLYGON_SVG], dtype=np.int32)
    clip_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
    cv2.fillPoly(clip_mask, [clip_pts], 255)

    for i, radius in enumerate(radii):
        if level == 0:
            color = _COLOR_GRAY_BGR
        else:
            color = active_bgr if i < level else _COLOR_GRAY_BGR
        ring_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
        cv2.circle(ring_mask, (cx, cy), radius, 255, stroke, lineType=cv2.LINE_AA)
        ring_mask = cv2.bitwise_and(ring_mask, clip_mask)
        _composite_stroke_mask(bgra, ring_mask, color)

    if level == 0:
        p1 = _svg_to_image_xy(*_WIFI_SLASH_SVG[0])
        p2 = _svg_to_image_xy(*_WIFI_SLASH_SVG[1])
        slash_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
        cv2.line(slash_mask, p1, p2, 255, slash_stroke, lineType=cv2.LINE_AA)
        _composite_stroke_mask(bgra, slash_mask, _COLOR_GRAY_BGR)


def _remove_svg_text(root: ET.Element) -> None:
    parents = _parent_map(root)
    to_remove = [el for el in root.iter() if el.tag.endswith("text")]
    for el in to_remove:
        parent = parents.get(el)
        if parent is not None:
            parent.remove(el)


def _draw_text_ops_bgra(bgra: np.ndarray, ops: list[_TextDrawOp]) -> None:
    digital7 = _resolve_settings_digital7_font()
    sharp = _resolve_settings_sharp_semibold_font()
    if digital7 is None and sharp is None:
        return

    rgb = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)

    for op in ops:
        path = sharp if op.sharp_semibold else digital7
        if path is None:
            path = digital7 or sharp
        if path is None:
            continue
        size_px = _svg_font_size_to_px(op.size_svg)
        font = _load_pil_font(str(path), size_px)
        x, y = _svg_to_image_xy(op.x_svg, op.y_svg)
        draw.text((x, y), op.text, font=font, fill=_hex_to_rgba(op.fill), anchor="ls")

    bgra[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA)


def default_settings_svg_path() -> Path:
    """Resolve ``settings_0.7.svg`` (override with ``PIGEON_SETTINGS_SVG``)."""
    import os

    env = os.environ.get("PIGEON_SETTINGS_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    # pigeonSystem/pigeon/widgets -> …/Pigeon/Pigeon_GFX/pigeonAI/
    pigeon_root = Path(__file__).resolve().parents[5]
    return pigeon_root / "Pigeon_GFX" / "pigeonAI" / "settings_0.7.svg"


def _find_by_id(root: ET.Element, layer_id: str) -> ET.Element | None:
    for el in root.iter():
        if el.get("id") == layer_id:
            return el
    return None


def _set_visible(el: ET.Element | None, visible: bool) -> None:
    if el is None:
        return
    if visible:
        el.attrib.pop("display", None)
    else:
        el.set("display", "none")


def _apply_contrast_color(group: ET.Element, *, selected: bool) -> None:
    """Green text/icons on black buttons; black on green when selected."""
    contrast = COLOR_BLACK if selected else COLOR_GREEN
    for node in group.iter():
        fill = (node.get("fill") or "").lower()
        stroke = (node.get("stroke") or "").lower()
        if fill in (COLOR_GREEN, COLOR_BLACK):
            node.set("fill", contrast)
        if stroke in (COLOR_GREEN, COLOR_BLACK):
            node.set("stroke", contrast)


def _apply_lock_icon_colors(group: ET.Element, *, selected: bool) -> None:
    """Lock body = filled rect; shackle = stroked circle only (no fill)."""
    contrast = COLOR_BLACK if selected else COLOR_GREEN
    for node in group.iter():
        if node.tag.endswith("rect"):
            node.set("fill", contrast)
            node.attrib.pop("stroke", None)
        elif node.tag.endswith("circle"):
            node.set("fill", "none")
            node.set("stroke", contrast)


def _apply_button_fill(group: ET.Element | None, *, selected: bool) -> None:
    if group is None:
        return
    fill = COLOR_GREEN if selected else COLOR_BLACK
    for node in group.iter():
        if not (node.tag.endswith("rect") or node.tag.endswith("path")):
            continue
        node_fill = (node.get("fill") or "").lower()
        if node_fill in (COLOR_GREEN, COLOR_BLACK, "none"):
            node.set("fill", fill)


def _device_variant_map(state: SettingsPageState) -> dict[str, str]:
    return {
        "deviceOne": state.device_one_variant,
        "deviceTwo": state.device_two_variant,
        "deviceThree": state.device_three_variant,
    }


def _visible_device_layers(state: SettingsPageState) -> set[str]:
    visible: set[str] = set()
    for device_key, active in _device_variant_map(state).items():
        visible.update(_DEVICE_VARIANT_LAYERS[device_key][active])
    return visible


def _apply_device_variants(root: ET.Element, state: SettingsPageState) -> None:
    for device_key, active in _device_variant_map(state).items():
        layers = _DEVICE_VARIANT_LAYERS[device_key]
        for variant, layer_ids in layers.items():
            show = variant == active
            for layer_id in layer_ids:
                _set_visible(_find_by_id(root, layer_id), show)


def _apply_wifi_icon(root: ET.Element, state: SettingsPageState) -> None:
    level = max(0, min(3, int(state.wifi_level)))
    for icon_id in _WIFI_ICON_IDS:
        _set_visible(_find_by_id(root, icon_id), icon_id == f"icon_WIFI_{level}")


def apply_settings_page_state(root: ET.Element, state: SettingsPageState) -> None:
    """Mutate an SVG element tree to match ``state``."""
    _apply_device_variants(root, state)
    _apply_wifi_icon(root, state)

    lock_el = _find_by_id(root, "icon_lock")
    _set_visible(lock_el, bool(state.network_locked))

    visible_device_layers = _visible_device_layers(state)

    for idx, button_id in enumerate(_BUTTON_LAYER_IDS):
        selected = idx == int(state.selected)
        button_el = _find_by_id(root, button_id)
        _apply_button_fill(button_el, selected=selected)

        for layer_id in _BUTTON_ASSOCIATED_TEXT[button_id]:
            if layer_id.startswith("icon_WIFI_"):
                continue
            if layer_id == "icon_lock" and not state.network_locked:
                continue
            if layer_id == "icon_lock":
                layer = _find_by_id(root, layer_id)
                if layer is not None and layer.get("display") != "none":
                    _apply_lock_icon_colors(layer, selected=selected)
                continue
            if layer_id in visible_device_layers or layer_id not in _ALL_DEVICE_LAYER_IDS:
                layer = _find_by_id(root, layer_id)
                if layer is not None and layer.get("display") != "none":
                    _apply_contrast_color(layer, selected=selected)

        if button_id == "selection_network":
            wifi_el = _find_by_id(root, f"icon_WIFI_{max(0, min(3, int(state.wifi_level)))}")
            if wifi_el is not None:
                _apply_contrast_color(wifi_el, selected=selected)


def _svg_tree_from_path(path: Path) -> ET.Element:
    tree = ET.parse(path)
    root = tree.getroot()
    root.set("viewBox", _VIEWBOX)
    root.set("width", str(DESIGN_W))
    root.set("height", str(DESIGN_H))
    return root


def _rasterize_svg_tree(root: ET.Element) -> np.ndarray:
    """Return BGRA uint8 (DESIGN_H × DESIGN_W). Uses PyMuPDF; cairosvg if available."""
    svg_bytes = ET.tostring(root, encoding="utf-8")

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=svg_bytes, filetype="svg")
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(DESIGN_W / page.rect.width, DESIGN_H / page.rect.height))
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGBA2BGRA)
        else:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGRA)
        if bgra.shape[0] != DESIGN_H or bgra.shape[1] != DESIGN_W:
            bgra = cv2.resize(bgra, (DESIGN_W, DESIGN_H), interpolation=cv2.INTER_AREA)
        return bgra
    except ImportError:
        pass

    try:
        import cairosvg

        out = io.BytesIO()
        cairosvg.svg2png(bytestring=svg_bytes, write_to=out, output_width=DESIGN_W, output_height=DESIGN_H)
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
        return bgra
    except OSError as exc:
        raise RuntimeError(
            "Settings page needs PyMuPDF (pip install pymupdf) or cairosvg with system cairo."
        ) from exc

    raise RuntimeError("Install pymupdf or cairosvg to rasterize the settings SVG.")


def render_settings_page_bgra(
    state: SettingsPageState | None = None,
    *,
    svg_path: Path | str | None = None,
) -> np.ndarray:
    """Load the settings SVG, apply ``state``, and return an 800×400 BGRA frame."""
    path = Path(svg_path) if svg_path is not None else default_settings_svg_path()
    if not path.is_file():
        raise FileNotFoundError(f"settings SVG not found: {path}")

    st = state if state is not None else SettingsPageState()
    root = _svg_tree_from_path(path)
    apply_settings_page_state(root, st)
    text_ops = _collect_text_draw_ops(root)
    _remove_svg_text(root)
    _remove_svg_wifi_icons(root)
    bgra = _rasterize_svg_tree(root)
    _draw_text_ops_bgra(bgra, text_ops)
    _draw_wifi_icon_overlay(bgra, st)
    return bgra
