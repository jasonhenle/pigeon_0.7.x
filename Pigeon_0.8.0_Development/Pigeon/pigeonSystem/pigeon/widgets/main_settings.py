"""
Pigeon 0.8 main settings — SVG chrome from ``settings_0.8/settings_main.svg``.

Renders the primary settings menu (800×480 BGRA) with left/right focus navigation
and selection recoloring. Text entry opens the shared settings keyboard overlay
(``settings_keyboard.py``).
"""

from __future__ import annotations

import io
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import IntEnum
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.font_paths import resolve_digital7_font, resolve_ui_font_medium

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

_MATRIX_RE = re.compile(
    r"matrix\(\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)",
    re.IGNORECASE,
)
_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-\d.]+)(?:[,\s]+([-\d.]+))?\s*\)",
    re.IGNORECASE,
)

# --- Colors (settingInstructions_0.8.0) ---
COLOR_SELECTED = "#FFFFFF"
COLOR_DESELECTED = "#000013"
COLOR_UI_DEFAULT = "#ff0013"
COLOR_ACCENT_DEFAULT = "#FFFFFF"
COLOR_INACTIVE = "#404040"
COLOR_VERSION_TEXT = "#000000"
COLOR_LEGACY_GREEN = "#02e900"

_ARTBOARD_H = 480.0
_WIFI_RADII_SVG: tuple[float, float, float] = (18.668, 29.008, 39.145)
_WIFI_STROKE_SVG = 7.0
_WIFI_FAIL_SIZE_SVG = 42.0
_COLOR_GRAY_BGR = (128, 128, 128)
_COLOR_GRAY_RGBA = (128, 128, 128, 255)
_TEXT_ENTRY_FIELDS: dict[str, dict[str, float | str | bool]] = {
    "location": {
        "text_id": "main_dual_location_text",
        "x0_svg": 82.0,
        "x1_svg": 383.5,
        "baseline_y_svg": 184.05,
        "font_size_svg": 35.0,
        "font": "digital7",
        "uppercase_only": True,
        "password_mask": False,
    },
    "network": {
        "text_id": "main_dual_network_name_text",
        "x0_svg": 417.0,
        "x1_svg": 717.8,
        "baseline_y_svg": 184.05,
        "font_size_svg": 35.0,
        "font": "sharp_medium",
        "uppercase_only": False,
        "password_mask": True,
        "placeholder": "password",
        "placeholder_font_size_svg": 30.0,
    },
}
_KEYBOARD_HIDE_WHEN_OPEN: tuple[str, ...] = (
    "main_exit_group",
    "main_box1_device_group",
    "main_box2_device_group",
    "main_box3_device_group",
    "main_box1_container",
    "main_box2_container",
    "main_box3_container",
)

# Keyboard SVG stubs (full systems not implemented yet).
KEYBOARD_SVG_NAMES: tuple[str, ...] = (
    "keyboard_bottom_row.svg",
    "keyboard_qwerty_lower.svg",
    "keyboard_qwerty_upper.svg",
    "keyboard_numeric_all.svg",
    "keyboard_numeric_pin.svg",
    "keyboard_symbolic.svg",
)
# Specified in instructions but not in the current GFX export set.
KEYBOARD_NUMERIC_IP_SVG = "keyboard_numeric_ip.svg"

_HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")
_AI_SUFFIX_RE = re.compile(r"_\d{20,}_?$")

# Primary nav ring (left/right). Network picker joins only when visible.
_PRIMARY_FOCUS_CANDIDATES: tuple[str, ...] = (
    "main_exit_button",
    "main_dual_location_button",
    "main_dual_network_button",
    "main_box1_button",
    "main_box2_button",
    "main_box3_button",
)

_ACTIVATE_ACTIONS: dict[str, str] = {
    "main_dual_location_button": "focus_location",
    "main_dual_network_button": "focus_network",
    "main_box1_button": "focus_box1",
    "main_box2_button": "focus_box2",
    "main_box3_button": "focus_box3",
    "main_exit_button": "exit",
    "main_network_picker_button": "focus_network_picker",
}

# Button → associated contrast layers (logical ids; AI suffixes tolerated).
_FOCUS_ASSOCIATED: dict[str, tuple[str, ...]] = {
    "main_dual_location_button": (
        "main_dual_location_text",
        "main_dual_locaion1_icon",
        "main_dual_locaion2_icon",
        "main_dual_locaion3_icon",
    ),
    "main_dual_network_button": (
        "main_dual_network_name_text",
    ),
    "main_box1_button": (),
    "main_box2_button": (),
    "main_box3_button": (),
    "main_exit_button": (
        "main_exit_text",
        "main_exit_icon",
    ),
    "main_network_picker_button": (
        "main_network_picker_up_icon",
        "main_network_picker_down_icon",
    ),
}

_PICKER_ROW_MINI_BUTTONS: tuple[str, ...] = (
    "main_network_picker_row1_mini_button",
    "main_network_picker_row2_mini_button",
    "main_network_picker_row3_mini_button",
)
_PICKER_ROW_TEXTS: tuple[str, ...] = (
    "main_network_picker_row1_text",
    "main_network_picker_row2_text",
    "main_network_picker_row3_text",
)
_PICKER_ROW_LOCK_GROUPS: tuple[str, ...] = (
    "main_network_picker_row1_lock_info",
    "main_network_picker_row22_lock_info",
    "main_network_picker_lock3_icon",
)

_HIDE_ALWAYS_LOGICAL: tuple[str, ...] = (
    "keyboardtemp",
)

_BACKGROUND_CONTAINERS: tuple[str, ...] = tuple(f"container{i}" for i in range(1, 8))

# Launch-visible layer groups (``settingInstructions_0.8.0``).
_LAUNCH_VISIBLE_LOGICAL: frozenset[str] = frozenset(
    {
        "settings_background",
        "main_exit_group",
        "main_dual",
        "main_box1_device_group",
        "main_box2_device_group",
        "main_box3_device_group",
    }
)

_BOX_LOCATION_GROUPS: tuple[str, ...] = (
    "main_box1_location_group",
    "main_box2_location_group",
    "main_box3_location_group",
)

# Rounded menu container from ``settings_main.svg`` clip path (800×480 artboard).
_MENU_CONTAINER_BBOX: tuple[int, int, int, int] = (22, 110, 777, 426)
_MENU_CONTAINER_RADIUS_PX: int = 20

# Box panel chrome (container, search, results) hidden until a box is opened.
_BOX_CHROME_PREFIXES: tuple[str, ...] = (
    "main_box1_container",
    "main_box1_search_results",
    "main_box1_search_icon",
    "main_box2_container",
    "main_box2_search_results",
    "main_box2_search_icon",
    "main_box3_container",
    "main_box3_search_results",
    "main_box3_search_icon",
)
_BOX_CHROME_NUM_RE = re.compile(r"^main_box([123])_")
# Rounded column chrome (button + accent) — always visible at launch.
_BOX_CONTAINER_LOGICALS: frozenset[str] = frozenset(
    {
        "main_box1_container",
        "main_box2_container",
        "main_box3_container",
    }
)
_BOX_DEVICE_GROUPS: dict[int, str] = {
    1: "main_box1_device_group",
    2: "main_box2_device_group",
    3: "main_box3_device_group",
}

_BUTTON_FILL_CANDIDATES = frozenset(
    {
        COLOR_SELECTED.lower(),
        COLOR_DESELECTED.lower(),
        COLOR_LEGACY_GREEN.lower(),
        "#ffffff",
        "#000013",
        "#000000",
        "#202020",
        "#231f20",  # Illustrator keyboard button default
        "white",
        "black",
    }
)
_CONTRAST_SWAP_CANDIDATES = frozenset(
    {
        COLOR_SELECTED.lower(),
        COLOR_DESELECTED.lower(),
        COLOR_LEGACY_GREEN.lower(),
        "#ffffff",
        "#000013",
        "#000000",
        "#202020",
        "white",
        "black",
    }
)

_UI_BRAND_COLORS = frozenset({COLOR_UI_DEFAULT.lower(), "#ff0013"})


def _is_ui_brand_color(hex_color: str | None) -> bool:
    if not hex_color:
        return False
    return hex_color.lower() in _UI_BRAND_COLORS


@dataclass(frozen=True)
class _ContainerStripeSpec:
    x_svg: float
    y_svg: float
    width_svg: float
    height_svg: float
    matrix: tuple[float, float, float, float, float, float]
    fill_hex: str


@dataclass(frozen=True)
class _WifiIconLayout:
    """Hex clip + ring geometry from ``settings_main.svg`` (PyMuPDF ignores clip-path)."""

    clip_polygon_svg: tuple[tuple[float, float], ...]
    cx_svg: float
    cy_svg: float
    fail_text_xy_svg: tuple[float, float] | None = None
    focus_logical: str = "main_dual_network_button"
    picker_row: bool = False


@dataclass(frozen=True)
class SettingsTheme:
    """User-themeable colors for main settings."""

    ui: str = COLOR_UI_DEFAULT
    selected: str = COLOR_SELECTED
    deselected: str = COLOR_DESELECTED
    inactive: str = COLOR_INACTIVE
    accent: str = COLOR_ACCENT_DEFAULT


class MainSettingsFocus(IntEnum):
    """Index into the live focus ring (see ``MainSettingsState.focus_ring``)."""

    FIRST = 0


@dataclass
class MainSettingsState:
    """Interactive state for the main settings SVG menu."""

    focus_index: int = 0
    theme: SettingsTheme = field(default_factory=SettingsTheme)
    wifi_level: int = 3  # stub 0–3
    location_name: str = "nest 1"
    network_name: str = "skynet"
    version_string: str = "0.8.0"
    show_network_picker: bool = False
    network_picker_row: int = 0
    show_instructions: bool = False
    show_box1_panel: bool = False
    show_box2_panel: bool = False
    show_box3_panel: bool = False
    # Focus ring rebuilt when panel visibility changes.
    focus_ring: tuple[str, ...] = field(default_factory=tuple)
    # Text-entry keyboard overlay (None = closed).
    keyboard: object | None = None

    @property
    def keyboard_open(self) -> bool:
        return self.keyboard is not None

    def ensure_focus_ring(self) -> None:
        ring = list(_PRIMARY_FOCUS_CANDIDATES)
        if self.show_network_picker:
            # Insert after dual network when present.
            if "main_dual_network_button" in ring:
                i = ring.index("main_dual_network_button") + 1
                ring.insert(i, "main_network_picker_button")
            else:
                ring.append("main_network_picker_button")
        self.focus_ring = tuple(ring)
        if not self.focus_ring:
            self.focus_ring = ("main_exit_button",)
        self.focus_index = int(self.focus_index) % len(self.focus_ring)

    @property
    def focused_id(self) -> str:
        self.ensure_focus_ring()
        return self.focus_ring[int(self.focus_index) % len(self.focus_ring)]

    def navigate(self, *, forward: bool = True) -> None:
        if self.keyboard is not None:
            self.keyboard.navigate(forward=forward)
            return
        self.ensure_focus_ring()
        n = len(self.focus_ring)
        step = 1 if forward else -1
        self.focus_index = (int(self.focus_index) + step) % n

    def open_keyboard(self, target: str, *, assets_dir: Path | str | None = None) -> None:
        from pigeon.widgets.settings_keyboard import open_keyboard

        if target == "location":
            initial = self.location_name
        elif target == "network":
            initial = self.network_name
        elif target == "pin":
            initial = ""
        else:
            initial = ""
        self.keyboard = open_keyboard(
            target=target,
            initial_text=initial,
            theme=self.theme,
            assets_dir=assets_dir,
        )

    def close_keyboard(self, *, commit: bool = False) -> str | None:
        """Close keyboard. If commit, write buffer into the target field. Returns target id."""
        kb = self.keyboard
        if kb is None:
            return None
        target = getattr(kb, "target", "") or ""
        if commit:
            text = str(getattr(kb, "buffer", "") or "")
            if target == "location":
                self.location_name = text
            elif target == "network":
                self.network_name = text
        self.keyboard = None
        return target or None


def decode_svg_id(raw_id: str) -> str:
    """Decode Illustrator XML id encoding (``_x5F_`` → ``_``, ``_x30_`` → ``0``, …)."""
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
            hx = raw_id[i + 2 : i + 4]
            try:
                out.append(chr(int(hx, 16)))
                i += 5
                continue
            except ValueError:
                pass
        out.append(raw_id[i])
        i += 1
    return "".join(out)


def encode_svg_id(logical_id: str) -> str:
    """Encode a logical layer id for Illustrator-style SVG ``id`` attributes."""
    if not logical_id:
        return ""
    body = logical_id
    if body.startswith("0"):
        body = "_x30_" + body[1:]

    def _enc_char(ch: str) -> str:
        if ch.isalnum() or ch in ".-|":
            return ch
        if ch == "_":
            return "_x5F_"
        return f"_x{ord(ch):02X}_"

    return "".join(_enc_char(c) for c in body)


def _strip_ai_uniqueness(decoded_id: str) -> str:
    """Drop Illustrator uniqueness suffixes like ``_0000016089…``."""
    return _AI_SUFFIX_RE.sub("", decoded_id)


def _normalize_logical(raw_or_logical: str) -> str:
    return _strip_ai_uniqueness(decode_svg_id(raw_or_logical))


def default_main_settings_svg_path(assets_dir: Path | str | None = None) -> Path:
    """Resolve ``settings_main.svg`` (override with ``PIGEON_MAIN_SETTINGS_SVG``)."""
    env = os.environ.get("PIGEON_MAIN_SETTINGS_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "settings_0.8" / "settings_main.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "settings_0.8" / "settings_main.svg"


def keyboard_svg_path(
    name: str,
    *,
    assets_dir: Path | str | None = None,
) -> Path:
    """Resolve a keyboard SVG under ``settings_0.8/`` (stub helper)."""
    base = Path(assets_dir) if assets_dir is not None else Path(__file__).resolve().parents[3] / "pigeonAssets"
    return Path(base) / "settings_0.8" / name


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent
    return parents


def _find_by_id(root: ET.Element, layer_id: str) -> ET.Element | None:
    for el in root.iter():
        if el.get("id") == layer_id:
            return el
    return None


def _find_by_logical_id(root: ET.Element, logical_id: str) -> ET.Element | None:
    """Match exact encoded id, raw logical id, or AI-suffixed variants."""
    want = _normalize_logical(logical_id)
    encoded = encode_svg_id(logical_id)
    # Prefer an exact logical id (e.g. ``main_box1`` group, not ``main_box1_right_icon``).
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        if _normalize_logical(raw) == want:
            return el
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        if raw == encoded or raw == logical_id:
            return el
        if raw.startswith(encoded + "_") and _AI_SUFFIX_RE.search("_" + raw[len(encoded) + 1 :]):
            return el
    return None


def _find_all_by_logical_id(root: ET.Element, logical_id: str) -> list[ET.Element]:
    want = _normalize_logical(logical_id)
    hits: list[ET.Element] = []
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        if _normalize_logical(raw) == want:
            hits.append(el)
            continue
        decoded = decode_svg_id(raw)
        if decoded.startswith(want + "_") and _AI_SUFFIX_RE.search(decoded[len(want) :]):
            hits.append(el)
    return hits


def _set_visible(el: ET.Element | None, visible: bool) -> None:
    """Toggle visibility on ``el`` and all descendants (PyMuPDF ignores parent display)."""
    if el is None:
        return
    for node in [el, *(child for child in el.iter() if child is not el)]:
        if visible:
            node.attrib.pop("display", None)
            style = node.get("style") or ""
            if "display:" in style:
                cleaned = re.sub(r"display\s*:\s*[^;]+;?\s*", "", style, flags=re.IGNORECASE)
                cleaned = cleaned.strip().rstrip(";")
                if cleaned:
                    node.set("style", cleaned)
                elif "style" in node.attrib:
                    node.attrib.pop("style")
        else:
            node.set("display", "none")


def _prune_display_none(root: ET.Element) -> None:
    """Remove ``display:none`` subtrees before rasterize (PyMuPDF ignores display)."""
    parents = _parent_map(root)
    to_remove = [el for el in root.iter() if el.get("display") == "none"]
    for el in to_remove:
        parent = parents.get(el)
        if parent is not None:
            try:
                parent.remove(el)
            except ValueError:
                pass


def _remove_by_logical_id(root: ET.Element, logical_id: str) -> None:
    """Remove nodes (PyMuPDF ignores display:none on some groups)."""
    parents = _parent_map(root)
    for el in list(_find_all_by_logical_id(root, logical_id)):
        parent = parents.get(el)
        if parent is not None:
            try:
                parent.remove(el)
            except ValueError:
                pass


def _norm_hex(color: str | None) -> str | None:
    if not color:
        return None
    c = color.strip().lower()
    if c in ("none", "transparent"):
        return c
    if c in ("white", "#fff"):
        return "#ffffff"
    if c in ("black", "#000"):
        return "#000000"
    if c.startswith("url("):
        return None
    if not c.startswith("#") and _HEX_RE.match(f"#{c}"):
        c = f"#{c}"
    if c.startswith("#") and len(c) == 4:
        c = f"#{c[1]}{c[1]}{c[2]}{c[2]}{c[3]}{c[3]}"
    if c.startswith("#") and len(c) == 7:
        return c
    return c


def _rewrite_style_prop(style: str, prop: str, value: str) -> str:
    pat = re.compile(rf"{re.escape(prop)}\s*:\s*[^;\"']+", re.IGNORECASE)
    if pat.search(style):
        return pat.sub(f"{prop}:{value}", style)
    if style.strip():
        return f"{style.rstrip().rstrip(';')};{prop}:{value}"
    return f"{prop}:{value}"


def _set_paint(node: ET.Element, *, fill: str | None = None, stroke: str | None = None) -> None:
    style = node.get("style") or ""
    if fill is not None:
        if "fill:" in style:
            style = _rewrite_style_prop(style, "fill", fill)
        node.set("fill", fill)
    if stroke is not None:
        if "stroke:" in style:
            style = _rewrite_style_prop(style, "stroke", stroke)
        node.set("stroke", stroke)
    if style:
        node.set("style", style)


def _iter_style_fill_stroke(node: ET.Element) -> tuple[str | None, str | None]:
    fill = _norm_hex(node.get("fill"))
    stroke = _norm_hex(node.get("stroke"))
    style = node.get("style") or ""
    if style:
        fm = re.search(r"fill\s*:\s*([^;\"']+)", style, re.IGNORECASE)
        sm = re.search(r"stroke\s*:\s*([^;\"']+)", style, re.IGNORECASE)
        if fm:
            fill = _norm_hex(fm.group(1).strip()) or fill
        if sm:
            stroke = _norm_hex(sm.group(1).strip()) or stroke
    return fill, stroke


def _style_prop(style: str | None, prop: str) -> str | None:
    if not style:
        return None
    m = re.search(rf"{re.escape(prop)}\s*:\s*([^;\"']+)", style, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip("'\"")


def _apply_button_fill(group: ET.Element | None, *, selected: bool, theme: SettingsTheme) -> None:
    if group is None:
        return
    fill = theme.selected if selected else theme.deselected
    for node in group.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("path", "rect", "polygon", "circle", "ellipse"):
            continue
        # Skip nested accent strokes that live under a *_button group by mistake.
        nid = _normalize_logical(node.get("id") or "")
        if nid.endswith("_accent") or "_accent_" in nid:
            continue
        cur_fill, _ = _iter_style_fill_stroke(node)
        if cur_fill is None or cur_fill in ("none", "transparent"):
            # Dual location/network use filled paths; still paint if attribute missing but path exists.
            if tag == "path" and (node.get("d") or "d" in (node.attrib or {})):
                if cur_fill in ("none", "transparent"):
                    continue
            else:
                continue
        if cur_fill in _BUTTON_FILL_CANDIDATES or cur_fill in (
            theme.selected.lower(),
            theme.deselected.lower(),
        ):
            _set_paint(node, fill=fill)


def _apply_contrast_paint(group: ET.Element | None, *, selected: bool, theme: SettingsTheme) -> None:
    """Contrasting text/icons on buttons (white on black, black on white). Leaves accent alone."""
    if group is None:
        return
    contrast = theme.deselected if selected else theme.selected
    for node in group.iter():
        nid = _normalize_logical(node.get("id") or "")
        if nid.endswith("_accent") or "_accent_" in nid:
            continue
        tag = node.tag.rsplit("}", 1)[-1]
        fill, stroke = _iter_style_fill_stroke(node)
        if tag.endswith("text") and fill in (None, "none", "transparent"):
            fill = "#ffffff"
        if (
            fill
            and fill not in ("none", "transparent")
            and fill in _CONTRAST_SWAP_CANDIDATES
            and not _is_ui_brand_color(fill)
        ):
            _set_paint(node, fill=contrast)
        if (
            stroke
            and stroke not in ("none", "transparent")
            and stroke in _CONTRAST_SWAP_CANDIDATES
            and not _is_ui_brand_color(stroke)
        ):
            _set_paint(node, stroke=contrast)


def _apply_accent_paint(group: ET.Element | None, accent: str) -> None:
    if group is None:
        return
    for node in group.iter():
        fill, stroke = _iter_style_fill_stroke(node)
        if fill and fill not in ("none", "transparent"):
            # Accents are typically stroked outlines; only replace white/theme-like fills.
            if fill in ("#ffffff", "white", COLOR_ACCENT_DEFAULT.lower()):
                _set_paint(node, fill=accent)
        if stroke and stroke not in ("none", "transparent"):
            if stroke in ("#ffffff", "white", COLOR_ACCENT_DEFAULT.lower()):
                _set_paint(node, stroke=accent)


def _set_text_content(el: ET.Element | None, text: str) -> None:
    if el is None:
        return
    # Prefer nested <text> if this is a group.
    texts = [n for n in el.iter() if n.tag.endswith("text")]
    targets = texts if texts else ([el] if el.tag.endswith("text") else [])
    for t in targets:
        if len(t):
            for child in list(t):
                if child.tag.endswith("tspan"):
                    child.text = text
                    child.tail = None
            if not any(c.tag.endswith("tspan") for c in t):
                t.text = text
        else:
            t.text = text


def _apply_keyboard_layer_visibility(root: ET.Element, state: MainSettingsState) -> None:
    """While the keyboard is open, hide chrome under the overlay (keep background + dual bar)."""
    if not state.keyboard_open:
        return
    for lid in _KEYBOARD_HIDE_WHEN_OPEN:
        _set_visible(_find_by_logical_id(root, lid), False)


def _text_field_spec(target: str) -> dict[str, float | str] | None:
    return _TEXT_ENTRY_FIELDS.get(target)


@lru_cache(maxsize=4)
def _load_digital7(size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = resolve_digital7_font()
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, max(6, size_px))
    except OSError:
        return ImageFont.load_default()


def _field_font_size_px(font_size_svg: float) -> int:
    return max(6, int(round(font_size_svg * DESIGN_H / _ARTBOARD_H)))


def _entry_content_font_size_px(
    spec: dict[str, float | str | bool],
    *,
    buffer: str,
    display: str,
) -> int:
    placeholder = str(spec.get("placeholder", "") or "")
    is_placeholder = (
        not buffer
        and spec.get("password_mask")
        and display == placeholder
    )
    if is_placeholder:
        ph_size = spec.get("placeholder_font_size_svg")
        if ph_size is not None:
            return _field_font_size_px(float(ph_size))
        return max(6, _field_font_size_px(float(spec["font_size_svg"])) - 5)
    return _field_font_size_px(float(spec["font_size_svg"]))


@lru_cache(maxsize=4)
def _load_sharp_medium(size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = resolve_ui_font_medium()
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, max(6, size_px))
    except OSError:
        return ImageFont.load_default()


def _field_font(spec: dict[str, float | str | bool], size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if spec.get("font") == "sharp_medium":
        return _load_sharp_medium(size_px)
    return _load_digital7(size_px)


def _password_mask_display(buffer: str) -> str:
    """Reveal only the most recently typed character (e.g. ``**g``)."""
    if not buffer:
        return ""
    if len(buffer) == 1:
        return buffer
    return ("*" * (len(buffer) - 1)) + buffer[-1]


def _entry_display_text(
    *,
    buffer: str,
    initial: str,
    spec: dict[str, float | str | bool],
) -> str:
    if buffer:
        if spec.get("password_mask"):
            return _password_mask_display(buffer)
        if spec.get("uppercase_only"):
            return buffer.upper()
        return buffer
    placeholder = str(spec.get("placeholder", "") or "")
    if spec.get("password_mask") and placeholder:
        return placeholder
    if initial:
        if spec.get("uppercase_only"):
            return initial.upper()
        return initial
    return ""


def _draw_text_entry_content(bgra: np.ndarray, state: MainSettingsState) -> None:
    """Paint centered dual-bar text while the keyboard is open (no cursor)."""
    kb = state.keyboard
    if kb is None:
        return
    target = str(getattr(kb, "target", "") or "")
    spec = _text_field_spec(target)
    if spec is None:
        return
    buffer = str(getattr(kb, "buffer", "") or "")
    initial = str(getattr(kb, "initial_text", "") or "")
    x0_px = int(round(float(spec["x0_svg"]) * DESIGN_W / 800.0))
    x1_px = int(round(float(spec["x1_svg"]) * DESIGN_W / 800.0))
    baseline_px = int(round(float(spec["baseline_y_svg"]) * DESIGN_H / _ARTBOARD_H))
    display = _entry_display_text(buffer=buffer, initial=initial, spec=spec)
    placeholder = str(spec.get("placeholder", "") or "")
    size_px = _entry_content_font_size_px(spec, buffer=buffer, display=display)
    font = _field_font(spec, size_px)
    rgb = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)
    cx = (x0_px + x1_px) // 2
    if display:
        is_placeholder = (
            not buffer
            and spec.get("password_mask")
            and display == placeholder
        )
        is_initial_gray = not buffer and bool(initial) and not spec.get("password_mask")
        fill = _COLOR_GRAY_RGBA if (is_placeholder or is_initial_gray) else (255, 255, 255, 255)
        bbox = draw.textbbox((0, 0), display, font=font, anchor="ls")
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, baseline_px), display, font=font, fill=fill, anchor="ls")
    bgra[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA)


def _draw_text_entry_cursor(bgra: np.ndarray, state: MainSettingsState) -> None:
    """Blinking insertion caret for the active dual-bar text field."""
    kb = state.keyboard
    if kb is None:
        return
    if int(time.monotonic() * 2) % 2:
        return
    target = str(getattr(kb, "target", "") or "")
    spec = _text_field_spec(target)
    if spec is None:
        return
    buffer = str(getattr(kb, "buffer", "") or "")
    initial = str(getattr(kb, "initial_text", "") or "")
    x0_px = int(round(float(spec["x0_svg"]) * DESIGN_W / 800.0))
    x1_px = int(round(float(spec["x1_svg"]) * DESIGN_W / 800.0))
    baseline_px = int(round(float(spec["baseline_y_svg"]) * DESIGN_H / _ARTBOARD_H))
    display = _entry_display_text(buffer=buffer, initial=initial, spec=spec)
    placeholder = str(spec.get("placeholder", "") or "")
    size_px = _entry_content_font_size_px(spec, buffer=buffer, display=display)
    font = _field_font(spec, size_px)
    rgb = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
    img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(img)
    cx = (x0_px + x1_px) // 2
    if display:
        bbox = draw.textbbox((0, 0), display, font=font, anchor="ls")
        tw = bbox[2] - bbox[0]
        if not buffer and spec.get("password_mask") and display == placeholder:
            cursor_x = cx - tw // 2
        else:
            cursor_x = cx - tw // 2 + tw + 2
    else:
        cursor_x = cx
    top = baseline_px - size_px + 4
    bot = baseline_px + 2
    draw.line([(cursor_x, top), (cursor_x, bot)], fill=(255, 255, 255, 255), width=2)
    bgra[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA)


def _layer_class(logical_id: str) -> str | None:
    for cls in ("_text", "_icon", "_button", "_accent", "_group", "_container"):
        if logical_id.endswith(cls) or f"{cls}_" in logical_id:
            # Prefer the terminal class token.
            if logical_id.endswith(cls):
                return cls
    for cls in ("_text", "_icon", "_button", "_accent", "_group", "_container"):
        if logical_id.endswith(cls):
            return cls
    return None


def _svg_scale(value: float) -> float:
    return value * DESIGN_H / _ARTBOARD_H


def _svg_to_px(x_svg: float, y_svg: float) -> tuple[int, int]:
    x = int(round(x_svg * DESIGN_W / 800.0))
    y = int(round(y_svg * DESIGN_H / _ARTBOARD_H))
    return x, y


def _svg_radius_to_px(radius_svg: float) -> int:
    return max(1, int(round(_svg_scale(radius_svg))))


def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#").lower()
    if len(h) == 3:
        h = f"{h[0]}{h[0]}{h[1]}{h[1]}{h[2]}{h[2]}"
    if len(h) != 6:
        return (255, 255, 255)
    return (int(h[2:4], 16), int(h[4:6], 16), int(h[0:2], 16))


def _hide_svg_wifi_icons(root: ET.Element) -> None:
    """Remove WiFi circles / fail badge from SVG (redrawn with star clip after rasterize)."""
    parents = _parent_map(root)
    to_remove: list[ET.Element] = []
    for el in root.iter():
        logical = _normalize_logical(el.get("id") or "")
        if re.search(r"wifi[123]_icon", logical) or "wifi_fail_icon" in logical:
            to_remove.append(el)
    for el in to_remove:
        parent = parents.get(el)
        if parent is not None:
            parent.remove(el)


def _svg_id_index(root: ET.Element) -> dict[str, ET.Element]:
    return {raw: el for el in root.iter() if (raw := el.get("id"))}


def _parse_matrix_translate(transform: str | None) -> tuple[float, float] | None:
    if not transform:
        return None
    m = _MATRIX_RE.search(transform)
    if m:
        return float(m.group(5)), float(m.group(6))
    t = _TRANSLATE_RE.search(transform)
    if t:
        return float(t.group(1)), float(t.group(2) or 0.0)
    return None


def _wifi_star_clip_polygon(group: ET.Element, id_index: dict[str, ET.Element]) -> tuple[tuple[float, float], ...]:
    """Illustrator ``<Star>`` clip mask → polygon points (the visible wifi wedge)."""
    xlink = f"{{{XLINK_NS}}}"
    for cp in group.iter():
        if not cp.tag.endswith("clipPath"):
            continue
        for child in cp:
            if child.tag.endswith("use"):
                href = child.get(f"{xlink}href") or child.get("href") or ""
                ref = href.lstrip("#")
                target = id_index.get(ref)
                if target is not None and target.get("points"):
                    pts = _parse_svg_points(target.get("points"))
                    if pts:
                        return pts
            if child.tag.endswith("polygon") and child.get("points"):
                pts = _parse_svg_points(child.get("points"))
                if pts:
                    return pts
    for poly in group.iter():
        if poly.tag.endswith("polygon") and poly.get("points"):
            pts = _parse_svg_points(poly.get("points"))
            if len(pts) >= 3:
                return pts
    return ()


def _wifi_circle_center(group: ET.Element) -> tuple[float, float] | None:
    for el in group.iter():
        if not el.tag.endswith("circle") or not el.get("cx"):
            continue
        logical = _normalize_logical(el.get("id") or "")
        if re.search(r"wifi[123]_icon", logical) or "wifi" in logical:
            return float(el.get("cx")), float(el.get("cy"))
    for el in group.iter():
        if el.tag.endswith("circle") and el.get("cx"):
            return float(el.get("cx")), float(el.get("cy"))
    return None


def _wifi_fail_text_xy(group: ET.Element) -> tuple[float, float] | None:
    for el in group.iter():
        logical = _normalize_logical(el.get("id") or "")
        if "wifi_fail_icon" not in logical:
            continue
        for text in el.iter():
            if text.tag.endswith("text"):
                pos = _parse_matrix_translate(text.get("transform"))
                if pos is not None:
                    return pos
    return None


def _discover_wifi_icon_layouts(root: ET.Element) -> list[_WifiIconLayout]:
    """Parse each wifi stack's star clip + circle center from ``settings_main.svg``."""
    id_index = _svg_id_index(root)
    parents = _parent_map(root)
    layouts: list[_WifiIconLayout] = []
    for group in root.iter():
        if not group.tag.endswith("g"):
            continue
        if _is_subtree_hidden(group, parents):
            continue
        logical = _normalize_logical(group.get("id") or "")
        is_dual = logical.endswith("_wifi_group")
        is_picker = "wifi_info" in logical and "network_picker" in logical
        if not is_dual and not is_picker:
            continue
        clip = _wifi_star_clip_polygon(group, id_index)
        center = _wifi_circle_center(group)
        if not clip or center is None:
            continue
        cx, cy = center
        if is_dual:
            focus_logical = "main_dual_network_button"
            picker_row = False
        else:
            focus_logical = "main_network_picker_button"
            picker_row = True
        layouts.append(
            _WifiIconLayout(
                clip_polygon_svg=clip,
                cx_svg=cx,
                cy_svg=cy,
                fail_text_xy_svg=_wifi_fail_text_xy(group),
                focus_logical=focus_logical,
                picker_row=picker_row,
            )
        )
    return layouts


def _composite_stroke_mask(
    dst: np.ndarray,
    mask: np.ndarray,
    color_bgr: tuple[int, int, int],
) -> None:
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


@lru_cache(maxsize=4)
def _load_wifi_fail_font(size_px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from pigeon.font_paths import resolve_ui_font_extrabold

    path = resolve_ui_font_extrabold()
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, max(8, size_px))
    except OSError:
        return ImageFont.load_default()


def _draw_wifi_fail_badge(
    bgra: np.ndarray,
    layout: _WifiIconLayout,
    *,
    color_bgr: tuple[int, int, int],
    clip_mask: np.ndarray,
) -> None:
    if layout.fail_text_xy_svg is None:
        return
    x, y = _svg_to_px(*layout.fail_text_xy_svg)
    size_px = max(8, int(round(_svg_scale(_WIFI_FAIL_SIZE_SVG))))
    font = _load_wifi_fail_font(size_px)
    text = "!"

    pad = max(48, size_px * 2)
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(bgra.shape[1], x + pad)
    y1 = min(bgra.shape[0], y + pad)
    patch_h, patch_w = y1 - y0, x1 - x0
    if patch_h <= 0 or patch_w <= 0:
        return

    rgb = np.zeros((patch_h, patch_w, 4), dtype=np.uint8)
    img = Image.fromarray(rgb, mode="RGBA")
    draw = ImageDraw.Draw(img)
    r, g, b = color_bgr[2], color_bgr[1], color_bgr[0]
    draw.text((x - x0, y - y0), text, font=font, fill=(r, g, b, 255), anchor="ls")
    glyph = np.asarray(img)
    glyph_mask = glyph[:, :, 3]

    region_clip = clip_mask[y0:y1, x0:x1]
    combined = cv2.bitwise_and(glyph_mask, region_clip)
    if not np.any(combined > 0):
        return

    sub = bgra[y0:y1, x0:x1]
    alpha = combined.astype(np.float32) / 255.0
    alpha3 = alpha[..., np.newaxis]
    fg = np.array(color_bgr, dtype=np.float32)
    bg = sub[:, :, :3].astype(np.float32)
    sub[:, :, :3] = np.clip(fg * alpha3 + bg * (1.0 - alpha3), 0, 255).astype(np.uint8)
    sub[:, :, 3] = 255


def _draw_wifi_icon_overlay(
    bgra: np.ndarray,
    layout: _WifiIconLayout,
    *,
    level: int,
    active_bgr: tuple[int, int, int],
) -> None:
    cx, cy = _svg_to_px(layout.cx_svg, layout.cy_svg)
    radii = [_svg_radius_to_px(r) for r in _WIFI_RADII_SVG]
    stroke = max(1, int(round(_svg_scale(_WIFI_STROKE_SVG))))

    clip_pts = np.array(
        [_svg_to_px(x, y) for x, y in layout.clip_polygon_svg],
        dtype=np.int32,
    )
    clip_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
    cv2.fillPoly(clip_mask, [clip_pts], 255)

    if level == 0:
        _draw_wifi_fail_badge(bgra, layout, color_bgr=active_bgr, clip_mask=clip_mask)
        return

    for i, radius in enumerate(radii):
        color = active_bgr if i < level else _COLOR_GRAY_BGR
        ring_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
        cv2.circle(ring_mask, (cx, cy), radius, 255, stroke, lineType=cv2.LINE_AA)
        ring_mask = cv2.bitwise_and(ring_mask, clip_mask)
        _composite_stroke_mask(bgra, ring_mask, color)


def _draw_wifi_overlays(
    bgra: np.ndarray,
    state: MainSettingsState,
    layouts: list[_WifiIconLayout],
    *,
    focused_logical: str,
) -> None:
    del focused_logical  # WiFi icons stay brand red regardless of nav focus.
    level = max(0, min(3, int(state.wifi_level)))
    active_bgr = _hex_to_bgr(state.theme.ui)
    for layout in layouts:
        if layout.picker_row and not state.show_network_picker:
            continue
        _draw_wifi_icon_overlay(bgra, layout, level=level, active_bgr=active_bgr)


@dataclass(frozen=True)
class _StarMaskedCircleSpec:
    """Circle stroke clipped by a hex/star polygon from the SVG (search + pigeon logo)."""

    mask_polygon_svg: tuple[tuple[float, float], ...]
    cx_svg: float
    cy_svg: float
    radius_svg: float
    stroke_svg: float
    default_stroke_hex: str
    focus_button: str | None = None
    circle_el_id: int | None = None


def _parse_svg_points(raw: str) -> tuple[tuple[float, float], ...]:
    nums = [float(x) for x in re.split(r"[\s,]+", raw.strip()) if x]
    if len(nums) < 6:
        return ()
    return tuple((nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2))


def _circle_stroke_from_element(el: ET.Element) -> tuple[float, float, float, float, str]:
    cx = float(el.get("cx") or 0.0)
    cy = float(el.get("cy") or 0.0)
    radius = float(el.get("r") or 0.0)
    _, stroke = _iter_style_fill_stroke(el)
    style = el.get("style") or ""
    sw_m = re.search(r"stroke-width\s*:\s*([-\d.]+)", style, re.IGNORECASE)
    if sw_m:
        stroke_w = float(sw_m.group(1))
    else:
        try:
            stroke_w = float(el.get("stroke-width") or 1.0)
        except ValueError:
            stroke_w = 1.0
    stroke_hex = stroke if stroke and stroke.startswith("#") else "#202020"
    return cx, cy, radius, stroke_w, stroke_hex


def _ancestor_search_icon(el: ET.Element, parents: dict[ET.Element, ET.Element]) -> ET.Element | None:
    cur: ET.Element | None = el
    while cur is not None:
        logical = _normalize_logical(cur.get("id") or "")
        if logical.endswith("_search_icon"):
            return cur
        cur = parents.get(cur)
    return None


def _box_focus_button(el: ET.Element, parents: dict[ET.Element, ET.Element]) -> str | None:
    cur: ET.Element | None = el
    while cur is not None:
        logical = _normalize_logical(cur.get("id") or "")
        for box in ("main_box1", "main_box2", "main_box3"):
            if logical == box or logical.startswith(f"{box}_"):
                return f"{box}_button"
        cur = parents.get(cur)
    return None


def _discover_star_masked_circles(root: ET.Element) -> list[_StarMaskedCircleSpec]:
    """Pair search star/triangle polygons with their ellipse circle strokes."""
    parents = _parent_map(root)
    specs: list[_StarMaskedCircleSpec] = []
    star_by_group_side: dict[tuple[int, str], tuple[tuple[float, float], ...]] = {}

    for el in root.iter():
        if not el.tag.endswith("polygon"):
            continue
        logical = _normalize_logical(el.get("id") or "")
        if "search" not in logical or "triangle" not in logical:
            continue
        side = "left" if "_left_" in logical else "right" if "_right_" in logical else None
        if side is None:
            continue
        pts = _parse_svg_points(el.get("points") or "")
        if not pts:
            continue
        search_group = _ancestor_search_icon(el, parents)
        if search_group is None or _is_subtree_hidden(el, parents):
            continue
        star_by_group_side[(id(search_group), side)] = pts

    for el in root.iter():
        if not el.tag.endswith("g"):
            continue
        logical = _normalize_logical(el.get("id") or "")
        if "search" not in logical or ("eplipse" not in logical and "ellipse" not in logical):
            continue
        side = "left" if "_left_" in logical else "right" if "_right_" in logical else None
        if side is None:
            continue
        search_group = _ancestor_search_icon(el, parents)
        if search_group is None or _is_subtree_hidden(el, parents):
            continue
        star_pts = star_by_group_side.get((id(search_group), side))
        if star_pts is None:
            continue
        circle_el: ET.Element | None = None
        for node in el.iter():
            if node.tag.endswith("circle") and node.get("cx") and node.get("r"):
                circle_el = node
                break
        if circle_el is None:
            continue
        cx, cy, radius, stroke_w, stroke_hex = _circle_stroke_from_element(circle_el)
        specs.append(
            _StarMaskedCircleSpec(
                mask_polygon_svg=star_pts,
                cx_svg=cx,
                cy_svg=cy,
                radius_svg=radius,
                stroke_svg=stroke_w,
                default_stroke_hex=stroke_hex,
                focus_button=_box_focus_button(search_group, parents),
                circle_el_id=id(circle_el),
            )
        )

    for el in root.iter():
        logical = _normalize_logical(el.get("id") or "")
        if not logical.endswith("_pigeon_logo_icon"):
            continue
        hex_pts: tuple[tuple[float, float], ...] | None = None
        circles: list[ET.Element] = []
        for sub in el.iter():
            if sub.tag.endswith("polygon") and sub.get("points"):
                pts = _parse_svg_points(sub.get("points") or "")
                if len(pts) == 6:
                    hex_pts = pts
            if sub.tag.endswith("circle") and sub.get("cx") and sub.get("r"):
                fill, stroke = _iter_style_fill_stroke(sub)
                if stroke in ("none", "transparent") and fill in ("none", "transparent"):
                    continue
                circles.append(sub)
        if hex_pts is None or not circles:
            continue
        focus_button = _box_focus_button(el, parents)
        for circle_el in circles:
            cx, cy, radius, stroke_w, stroke_hex = _circle_stroke_from_element(circle_el)
            specs.append(
                _StarMaskedCircleSpec(
                    mask_polygon_svg=hex_pts,
                    cx_svg=cx,
                    cy_svg=cy,
                    radius_svg=radius,
                    stroke_svg=stroke_w,
                    default_stroke_hex=stroke_hex,
                    focus_button=focus_button,
                    circle_el_id=id(circle_el),
                )
            )
    return specs


def _hide_star_masked_svg_circles(root: ET.Element, specs: list[_StarMaskedCircleSpec]) -> None:
    """Hide circle strokes replaced by star/hex clip overlays."""
    hide_ids = {spec.circle_el_id for spec in specs if spec.circle_el_id is not None}
    for el in root.iter():
        if el.tag.endswith("circle") and id(el) in hide_ids:
            _set_visible(el, False)


def _stroke_bgr_for_star_spec(
    spec: _StarMaskedCircleSpec,
    *,
    state: MainSettingsState,
    focused_logical: str,
) -> tuple[int, int, int]:
    theme = state.theme
    if spec.focus_button:
        selected = focused_logical == spec.focus_button
        contrast = theme.deselected if selected else theme.selected
        return _hex_to_bgr(contrast)
    return _hex_to_bgr(spec.default_stroke_hex)


def _draw_star_masked_circle_stroke(
    bgra: np.ndarray,
    spec: _StarMaskedCircleSpec,
    *,
    color_bgr: tuple[int, int, int],
) -> None:
    cx, cy = _svg_to_px(spec.cx_svg, spec.cy_svg)
    radius = _svg_radius_to_px(spec.radius_svg)
    stroke = max(1, int(round(_svg_scale(spec.stroke_svg))))
    clip_pts = np.array(
        [_svg_to_px(x, y) for x, y in spec.mask_polygon_svg],
        dtype=np.int32,
    )
    clip_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
    cv2.fillPoly(clip_mask, [clip_pts], 255)
    ring_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
    cv2.circle(ring_mask, (cx, cy), radius, 255, stroke, lineType=cv2.LINE_AA)
    ring_mask = cv2.bitwise_and(ring_mask, clip_mask)
    _composite_stroke_mask(bgra, ring_mask, color_bgr)


def _draw_star_masked_circle_overlays(
    bgra: np.ndarray,
    state: MainSettingsState,
    specs: list[_StarMaskedCircleSpec],
    *,
    focused_logical: str,
) -> None:
    for spec in specs:
        color = _stroke_bgr_for_star_spec(spec, state=state, focused_logical=focused_logical)
        _draw_star_masked_circle_stroke(bgra, spec, color_bgr=color)


def _is_subtree_hidden(el: ET.Element, parents: dict[ET.Element, ET.Element]) -> bool:
    cur: ET.Element | None = el
    while cur is not None:
        if cur.get("display") == "none":
            return True
        style = cur.get("style") or ""
        if re.search(r"display\s*:\s*none", style, re.IGNORECASE):
            return True
        cur = parents.get(cur)
    return False


def _active_background_container(state: MainSettingsState, focused: str) -> str:
    """Which diagonal-stripe ``containerN`` group matches the current panel."""
    if state.show_network_picker:
        return "container1"
    if focused == "main_box3_button" or state.show_box3_panel:
        return "container3"
    if focused == "main_box2_button" or state.show_box2_panel:
        return "container2"
    if focused == "main_box1_button" or state.show_box1_panel:
        return "container1"
    return "container1"


def _apply_background_container(
    root: ET.Element,
    state: MainSettingsState,
    *,
    focused: str,
) -> None:
    active = _active_background_container(state, focused)
    for cid in _BACKGROUND_CONTAINERS:
        _set_visible(_find_by_logical_id(root, cid), cid == active)


def _style_has_display_none(style: str | None) -> bool:
    return bool(style and re.search(r"display\s*:\s*none", style, re.IGNORECASE))


def _parse_svg_matrix(transform: str | None) -> tuple[float, float, float, float, float, float] | None:
    if not transform:
        return None
    m = _MATRIX_RE.search(transform)
    if not m:
        return None
    return tuple(float(m.group(i)) for i in range(1, 7))


def _transform_rect_corners_svg(
    x: float,
    y: float,
    w: float,
    h: float,
    matrix: tuple[float, float, float, float, float, float],
) -> tuple[tuple[float, float], ...]:
    a, b, c, d, e, f = matrix
    corners = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    out: list[tuple[float, float]] = []
    for px, py in corners:
        out.append((a * px + c * py + e, b * px + d * py + f))
    return tuple(out)


def _menu_container_mask() -> np.ndarray:
    """Inside=255 mask for the red menu container (PyMuPDF inverts SVG clip-path)."""
    from PIL import Image, ImageDraw

    mask = Image.new("L", (DESIGN_W, DESIGN_H), 0)
    draw = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = _MENU_CONTAINER_BBOX
    draw.rounded_rectangle((x0, y0, x1, y1), radius=_MENU_CONTAINER_RADIUS_PX, fill=255)
    return np.asarray(mask, dtype=np.uint8)


def _discover_container_stripe_specs(root: ET.Element, container_id: str) -> tuple[_ContainerStripeSpec, ...]:
    container = _find_by_logical_id(root, container_id)
    if container is None:
        return ()
    specs: list[_ContainerStripeSpec] = []
    for el in container.iter():
        if not el.tag.endswith("rect"):
            continue
        style = el.get("style") or ""
        if _style_has_display_none(style) or el.get("display") == "none":
            continue
        transform = el.get("transform") or _style_prop(style, "transform")
        matrix = _parse_svg_matrix(transform)
        if matrix is None:
            continue
        fill = _style_prop(style, "fill") or el.get("fill") or "#ff0013"
        if not fill.startswith("#"):
            continue
        try:
            specs.append(
                _ContainerStripeSpec(
                    x_svg=float(el.get("x") or 0.0),
                    y_svg=float(el.get("y") or 0.0),
                    width_svg=float(el.get("width") or 0.0),
                    height_svg=float(el.get("height") or 0.0),
                    matrix=matrix,
                    fill_hex=fill.lower(),
                )
            )
        except (TypeError, ValueError):
            continue
    return tuple(specs)


def _hide_container_stripe_rects(root: ET.Element) -> None:
    """Remove diagonal stripe rects and dim overlay plates before rasterize."""
    for cid in _BACKGROUND_CONTAINERS:
        container = _find_by_logical_id(root, cid)
        if container is None:
            continue
        parents = _parent_map(root)
        for el in list(container.iter()):
            if not el.tag.endswith("rect"):
                continue
            style = el.get("style") or ""
            transform = el.get("transform") or _style_prop(style, "transform")
            if _parse_svg_matrix(transform) is not None:
                parent = parents.get(el)
                if parent is not None:
                    parent.remove(el)
                continue
            fill = _style_prop(style, "fill") or el.get("fill") or ""
            opacity = _style_prop(style, "opacity") or ""
            if _norm_hex(fill) == "#202020" or opacity.startswith("0.5"):
                parent = parents.get(el)
                if parent is not None:
                    parent.remove(el)


def _remove_canvas_background_rect(root: ET.Element) -> None:
    """Drop the full-canvas rect so PyMuPDF leaves transparency for compositing."""
    bg_group = _find_by_logical_id(root, "background")
    if bg_group is None:
        return
    parents = _parent_map(root)
    for el in list(bg_group.iter()):
        if el.tag.endswith("rect"):
            parent = parents.get(el)
            if parent is not None:
                parent.remove(el)


def _draw_container_background_bgra(bgra: np.ndarray, stripes: tuple[_ContainerStripeSpec, ...]) -> None:
    """Paint clipped diagonal stripes behind UI (fixes inverted PyMuPDF clip-path)."""
    if not stripes:
        return
    mask = _menu_container_mask()
    for stripe in stripes:
        corners = _transform_rect_corners_svg(
            stripe.x_svg,
            stripe.y_svg,
            stripe.width_svg,
            stripe.height_svg,
            stripe.matrix,
        )
        pts = np.array([_svg_to_px(x, y) for x, y in corners], dtype=np.int32)
        poly_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
        cv2.fillConvexPoly(poly_mask, pts, 255)
        poly_mask = cv2.bitwise_and(poly_mask, mask)
        _composite_stroke_mask(bgra, poly_mask, _hex_to_bgr(stripe.fill_hex))


def _composite_bgra_over_bgra(base: np.ndarray, overlay: np.ndarray) -> np.ndarray:
    fg = overlay.astype(np.float32)
    bg = base.astype(np.float32)
    alpha = fg[:, :, 3:4] / 255.0
    inv = 1.0 - alpha
    out = bg.copy()
    out[:, :, :3] = fg[:, :, :3] * alpha + bg[:, :, :3] * inv
    out[:, :, 3] = np.clip(fg[:, :, 3] + bg[:, :, 3] * inv[..., 0], 0, 255)
    return out.astype(np.uint8)


def _is_box_chrome_logical(logical: str) -> bool:
    if logical in _BOX_CONTAINER_LOGICALS:
        return True
    return any(
        logical == prefix or logical.startswith(f"{prefix}_")
        for prefix in _BOX_CHROME_PREFIXES
    )


def _is_device_label_logical(logical: str) -> bool:
    """Primary device name / IP labels inside a box column."""
    if not logical:
        return False
    if logical.endswith("_device_name_text") or logical.endswith("_device_ip_text"):
        return True
    return logical in ("main_device_name_text", "main_device_ip_text")


def _find_box_device_group(root: ET.Element, box_num: int) -> ET.Element | None:
    want = _BOX_DEVICE_GROUPS.get(box_num, "")
    if not want:
        return None
    hit = _find_by_logical_id(root, want)
    if hit is not None:
        return hit
    prefix = f"main_box{box_num}_device_group"
    for el in root.iter():
        logical = _normalize_logical(el.get("id") or "")
        if logical == prefix or logical.startswith(f"{prefix}_"):
            return el
    return None


def _apply_box_device_text_contrast(
    root: ET.Element,
    *,
    box_num: int,
    selected: bool,
    theme: SettingsTheme,
) -> None:
    """When a box nav item is selected, invert device name + IP inside that column."""
    group = _find_box_device_group(root, box_num)
    if group is None:
        return
    for el in group.iter():
        logical = _normalize_logical(el.get("id") or "")
        if not _is_device_label_logical(logical):
            continue
        _apply_contrast_paint(el, selected=selected, theme=theme)


def _box_num_from_chrome_logical(logical: str) -> int | None:
    m = _BOX_CHROME_NUM_RE.match(logical)
    return int(m.group(1)) if m else None


def _apply_scene_layer_visibility(root: ET.Element, state: MainSettingsState) -> None:
    """
    Default view shows only launch layers (exit, dual bar, box device groups, background).

    Location groups, instructions, network picker, and box panel chrome open via state flags.
    """
    _set_visible(_find_by_logical_id(root, "main_instructions"), state.show_instructions)
    _set_visible(_find_by_logical_id(root, "main_network_picker"), state.show_network_picker)

    if state.show_network_picker:
        for lid in ("main_box2_add_search_icon", "main_box1_text"):
            _set_visible(_find_by_logical_id(root, lid), False)

    panel_open = {
        1: bool(state.show_box1_panel),
        2: bool(state.show_box2_panel),
        3: bool(state.show_box3_panel),
    }
    for i, gid in enumerate(_BOX_LOCATION_GROUPS, start=1):
        _set_visible(_find_by_logical_id(root, gid), panel_open.get(i, False))

    for el in root.iter():
        if not el.tag.endswith("g"):
            continue
        raw = el.get("id") or ""
        if not raw:
            continue
        logical = _normalize_logical(raw)
        if logical in _BOX_CONTAINER_LOGICALS:
            _set_visible(el, True)
            continue
        if not _is_box_chrome_logical(logical):
            continue
        box_num = _box_num_from_chrome_logical(logical)
        _set_visible(el, bool(box_num and panel_open.get(box_num, False)))


def _apply_network_picker_rows(
    root: ET.Element,
    state: MainSettingsState,
    *,
    theme: SettingsTheme,
) -> None:
    """Per-row white/black pills inside the picker (outer container stays dark)."""
    if not state.show_network_picker:
        return
    row_idx = max(0, min(len(_PICKER_ROW_MINI_BUTTONS) - 1, int(state.network_picker_row)))
    for i, mini_id in enumerate(_PICKER_ROW_MINI_BUTTONS):
        selected = i == row_idx
        mini_el = _find_by_logical_id(root, mini_id)
        _apply_button_fill(mini_el, selected=selected, theme=theme)
        for hit in _find_all_by_logical_id(root, mini_id):
            _apply_button_fill(hit, selected=selected, theme=theme)

    for i, text_id in enumerate(_PICKER_ROW_TEXTS):
        selected = i == row_idx
        for text_el in _find_all_by_logical_id(root, text_id):
            _apply_contrast_paint(text_el, selected=selected, theme=theme)

    for i, lock_id in enumerate(_PICKER_ROW_LOCK_GROUPS):
        selected = i == row_idx
        for lock_el in _find_all_by_logical_id(root, lock_id):
            _apply_contrast_paint(lock_el, selected=selected, theme=theme)


def discover_focus_ring_in_svg(root: ET.Element, state: MainSettingsState) -> tuple[str, ...]:
    """Build a focus ring from candidates that exist in the SVG tree."""
    state.ensure_focus_ring()
    present: list[str] = []
    for logical in state.focus_ring:
        if _find_by_logical_id(root, logical) is not None:
            present.append(logical)
    if not present:
        # Absolute fallback.
        for logical in ("main_exit_button", *_PRIMARY_FOCUS_CANDIDATES):
            if _find_by_logical_id(root, logical) is not None and logical not in present:
                present.append(logical)
    return tuple(present) if present else ("main_exit_button",)


def apply_main_settings_svg_state(root: ET.Element, state: MainSettingsState) -> None:
    """Mutate SVG tree: selection fills, contrast text/icons, accents, visibility."""
    theme = state.theme
    state.ensure_focus_ring()

    for hid in _HIDE_ALWAYS_LOGICAL:
        _remove_by_logical_id(root, hid)

    picker = _find_by_logical_id(root, "main_network_picker")
    _set_visible(picker, bool(state.show_network_picker))
    _apply_scene_layer_visibility(root, state)
    _apply_keyboard_layer_visibility(root, state)

    kb_target = ""
    if state.keyboard is not None:
        kb_target = str(getattr(state.keyboard, "target", "") or "")

    # Apply theme accent globally to *_accent layers (selection does not change accent).
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        logical = _normalize_logical(raw)
        if logical.endswith("_accent") or re.search(r"_accent(_|$)", logical):
            _apply_accent_paint(el, theme.accent)

    # Dynamic text stubs.
    if not (state.keyboard_open and kb_target == "location"):
        _set_text_content(_find_by_logical_id(root, "main_dual_location_text"), state.location_name)
    else:
        _set_visible(_find_by_logical_id(root, "main_dual_location_text"), False)
    if not (state.keyboard_open and kb_target == "network"):
        _set_text_content(_find_by_logical_id(root, "main_dual_network_name_text"), state.network_name)
    else:
        _set_visible(_find_by_logical_id(root, "main_dual_network_name_text"), False)
    for ver_id in ("text_pigeonVersion", "main_pigeon_version_text", "pigeon_version_text"):
        ver_el = _find_by_logical_id(root, ver_id)
        if ver_el is not None:
            _set_text_content(ver_el, state.version_string)
            _apply_contrast_paint(ver_el, selected=True, theme=SettingsTheme(
                selected=COLOR_VERSION_TEXT,
                deselected=COLOR_VERSION_TEXT,
            ))
            # Force black regardless of selection.
            for node in ver_el.iter():
                fill, stroke = _iter_style_fill_stroke(node)
                if fill and fill not in ("none", "transparent"):
                    _set_paint(node, fill=COLOR_VERSION_TEXT)
                if stroke and stroke not in ("none", "transparent"):
                    _set_paint(node, stroke=COLOR_VERSION_TEXT)

    focused = "" if state.keyboard_open else state.focused_id
    ring = discover_focus_ring_in_svg(root, state)
    if not state.keyboard_open and focused not in ring and ring:
        # Snap to a ring entry that exists.
        state.focus_ring = ring
        state.focus_index = 0
        focused = state.focused_id

    _apply_background_container(root, state, focused=focused or state.focused_id)
    _apply_network_picker_rows(root, state, theme=theme)

    for logical in ring:
        selected = (not state.keyboard_open) and logical == focused
        if logical == "main_network_picker_button":
            # Outer picker shell stays dark; row mini buttons handle selection fill.
            continue
        button_el = _find_by_logical_id(root, logical)
        _apply_button_fill(button_el, selected=selected, theme=theme)

        # Also paint direct path children named the same logical button.
        for hit in _find_all_by_logical_id(root, logical):
            _apply_button_fill(hit, selected=selected, theme=theme)

        for assoc in _FOCUS_ASSOCIATED.get(logical, ()):
            for assoc_el in _find_all_by_logical_id(root, assoc):
                cls = _layer_class(_normalize_logical(assoc_el.get("id") or assoc))
                if cls == "_accent":
                    continue
                _apply_contrast_paint(assoc_el, selected=selected, theme=theme)

        if logical in ("main_box1_button", "main_box2_button", "main_box3_button"):
            box_num = int(logical[8])
            _apply_box_device_text_contrast(
                root, box_num=box_num, selected=selected, theme=theme
            )

        # Exit text lives under main_exit_icon; ensure EXIT contrast.
        if logical == "main_exit_button":
            for assoc_el in _find_all_by_logical_id(root, "main_exit_text"):
                _apply_contrast_paint(assoc_el, selected=selected, theme=theme)


def _svg_tree_from_path(path: Path) -> ET.Element:
    tree = ET.parse(path)
    root = tree.getroot()
    # Native 800×480 artboard — matches pigeon.design canvas (full bleed, no letterbox).
    root.set("viewBox", "0 0 800 480")
    root.set("width", str(DESIGN_W))
    root.set("height", str(DESIGN_H))
    return root


def _rasterize_svg_tree(root: ET.Element) -> np.ndarray:
    """Return BGRA uint8 (DESIGN_H × DESIGN_W) with Digital-7 / Sharp Sans labels."""
    from pigeon.widgets.settings_svg_text import rasterize_settings_svg_bgra

    return rasterize_settings_svg_bgra(root, width=DESIGN_W, height=DESIGN_H)


def render_main_settings_bgra(
    state: MainSettingsState | None = None,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    """Load settings_main.svg, apply ``state``, return 800×480 BGRA."""
    if svg_path is not None:
        path = Path(svg_path)
    else:
        path = default_main_settings_svg_path(assets_dir)
    if not path.is_file():
        raise FileNotFoundError(f"main settings SVG not found: {path}")

    st = state if state is not None else MainSettingsState()
    st.ensure_focus_ring()
    root = _svg_tree_from_path(path)
    # Narrow focus ring to layers that exist in this SVG.
    present = discover_focus_ring_in_svg(root, st)
    st.focus_ring = present
    st.focus_index = int(st.focus_index) % max(1, len(present))
    apply_main_settings_svg_state(root, st)
    focused_logical = "" if st.keyboard_open else st.focused_id
    active_container = _active_background_container(st, focused_logical or st.focused_id)
    stripe_specs = _discover_container_stripe_specs(root, active_container)
    _hide_container_stripe_rects(root)
    _remove_canvas_background_rect(root)
    star_specs = _discover_star_masked_circles(root)
    wifi_layouts = _discover_wifi_icon_layouts(root)
    _hide_svg_wifi_icons(root)
    _hide_star_masked_svg_circles(root, star_specs)
    _prune_display_none(root)
    ui_bgra = _rasterize_svg_tree(root)
    bg_bgra = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
    bg_bgra[:, :, :3] = 0
    bg_bgra[:, :, 3] = 255
    _draw_container_background_bgra(bg_bgra, stripe_specs)
    bgra = _composite_bgra_over_bgra(bg_bgra, ui_bgra)
    _draw_wifi_overlays(bgra, st, wifi_layouts, focused_logical=focused_logical)
    _draw_star_masked_circle_overlays(bgra, st, star_specs, focused_logical=focused_logical)
    if st.keyboard_open:
        _draw_text_entry_content(bgra, st)
    return bgra


class MainSettingsWidget:
    """Cached main-settings compositor (similar to ``NowPlayingScreenWidget``)."""

    def __init__(
        self,
        *,
        assets_dir: Path | str | None = None,
        svg_path: Path | str | None = None,
        state: MainSettingsState | None = None,
    ) -> None:
        self._assets_dir = Path(assets_dir) if assets_dir is not None else None
        self._svg_path = Path(svg_path) if svg_path is not None else None
        self._state = state if state is not None else MainSettingsState()
        self._state.ensure_focus_ring()
        self._cached_bgra: np.ndarray | None = None
        self._cached_sig: tuple[object, ...] | None = None
        self._cached_main_bgra: np.ndarray | None = None
        self._cached_main_sig: tuple[object, ...] | None = None
        self._cached_kb_bgra: np.ndarray | None = None
        self._cached_kb_sig: tuple[object, ...] | None = None

    @property
    def state(self) -> MainSettingsState:
        return self._state

    def invalidate(self) -> None:
        self._cached_bgra = None
        self._cached_sig = None
        self._cached_main_bgra = None
        self._cached_main_sig = None
        self._cached_kb_bgra = None
        self._cached_kb_sig = None

    def _invalidate_keyboard_cache(self) -> None:
        self._cached_kb_bgra = None
        self._cached_kb_sig = None
        self._cached_bgra = None
        self._cached_sig = None

    def _main_state_sig(self) -> tuple[object, ...]:
        st = self._state
        th = st.theme
        kb = st.keyboard
        kb_main: tuple[object, ...] = ()
        if kb is not None:
            kb_main = (
                getattr(kb, "mode", None),
                str(getattr(kb, "buffer", "")),
                str(getattr(kb, "target", "")),
                str(getattr(kb, "initial_text", "")),
                bool(getattr(kb, "supports_lowercase", True)),
            )
        return (
            int(st.focus_index) if not st.keyboard_open else -1,
            st.focus_ring,
            th.ui,
            th.selected,
            th.deselected,
            th.inactive,
            th.accent,
            int(st.wifi_level),
            st.location_name,
            st.network_name,
            st.version_string,
            bool(st.show_network_picker),
            int(st.network_picker_row),
            bool(st.show_instructions),
            bool(st.show_box1_panel),
            bool(st.show_box2_panel),
            bool(st.show_box3_panel),
            bool(st.keyboard_open),
            str(self._svg_path or ""),
            str(self._assets_dir or ""),
            kb_main,
        )

    def _keyboard_overlay_sig(self) -> tuple[object, ...] | None:
        kb = self._state.keyboard
        if kb is None:
            return None
        return (
            getattr(kb, "mode", None),
            int(getattr(kb, "focus_index", 0)),
            str(self._assets_dir or ""),
        )

    def navigate(self, forward: bool = True) -> None:
        self._state.navigate(forward=forward)
        if self._state.keyboard is not None:
            self._invalidate_keyboard_cache()
        else:
            self.invalidate()

    def activate(self) -> str:
        """Return an action string for the focused control."""
        st = self._state
        if st.keyboard is not None:
            from pigeon.widgets.settings_keyboard import activate_key

            result = activate_key(st.keyboard, assets_dir=self._assets_dir)
            if result == "typing":
                self._cached_main_bgra = None
                self._cached_main_sig = None
                self._cached_bgra = None
            elif result.startswith("mode:"):
                self.invalidate()
            else:
                self.invalidate()
            if result == "cancel":
                st.close_keyboard(commit=False)
                self.invalidate()
                return "keyboard_cancel"
            if result == "go":
                target = st.close_keyboard(commit=True)
                self.invalidate()
                return f"keyboard_go:{target or ''}"
            return f"keyboard:{result}"

        focused = st.focused_id
        action = _ACTIVATE_ACTIONS.get(focused, f"activate:{focused}")
        if action == "focus_location":
            st.open_keyboard("location", assets_dir=self._assets_dir)
            self.invalidate()
            return "keyboard_open:location"
        if action == "focus_network":
            st.open_keyboard("network", assets_dir=self._assets_dir)
            self.invalidate()
            return "keyboard_open:network"
        if action == "focus_box1":
            st.show_box1_panel = True
            self.invalidate()
            return action
        if action == "focus_box2":
            st.show_box2_panel = True
            self.invalidate()
            return action
        if action == "focus_box3":
            st.show_box3_panel = True
            self.invalidate()
            return action
        return action

    def bgra_frame(self) -> np.ndarray | None:
        try:
            main_sig = self._main_state_sig()
            if self._cached_main_bgra is not None and self._cached_main_sig == main_sig:
                frame = self._cached_main_bgra
            else:
                frame = render_main_settings_bgra(
                    self._state,
                    svg_path=self._svg_path,
                    assets_dir=self._assets_dir,
                )
                self._cached_main_bgra = frame
                self._cached_main_sig = main_sig

            if self._state.keyboard is not None:
                from pigeon.widgets.settings_keyboard import render_keyboard_bgra

                kb_sig = self._keyboard_overlay_sig()
                if self._cached_kb_bgra is not None and self._cached_kb_sig == kb_sig:
                    kb_frame = self._cached_kb_bgra
                else:
                    kb_frame = render_keyboard_bgra(
                        self._state.keyboard,
                        assets_dir=self._assets_dir,
                    )
                    self._cached_kb_bgra = kb_frame
                    self._cached_kb_sig = kb_sig
                frame = frame.copy()
                base_bgr = frame[:, :, :3]
                blended = alpha_blend_bgra_over_bgr(base_bgr, kb_frame)
                alpha = kb_frame[:, :, 3:4].astype(np.float32) / 255.0
                out_a = np.clip(
                    alpha * 255.0 + (1.0 - alpha) * frame[:, :, 3:4].astype(np.float32),
                    0,
                    255,
                ).astype(np.uint8)
                frame = np.dstack([blended, out_a[:, :, 0]])
        except Exception:
            return self._cached_bgra
        self._cached_bgra = frame
        return frame

    def render(self, canvas_bgr: np.ndarray) -> None:
        """Paste main settings onto the design canvas (800×480, uniform scale only)."""
        frame = self.bgra_frame()
        if frame is None or canvas_bgr is None or canvas_bgr.size == 0:
            return
        if self._state.keyboard is not None:
            frame = frame.copy()
            _draw_text_entry_cursor(frame, self._state)
        ch, cw = int(canvas_bgr.shape[0]), int(canvas_bgr.shape[1])
        fh, fw = int(frame.shape[0]), int(frame.shape[1])
        if fh == ch and fw == cw:
            canvas_bgr[:] = alpha_blend_bgra_over_bgr(canvas_bgr, frame)
            return
        scale = min(cw / float(fw), ch / float(fh))
        tw = max(1, int(round(fw * scale)))
        th = max(1, int(round(fh * scale)))
        resized = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
        x0 = max(0, (cw - tw) // 2)
        y0 = max(0, (ch - th) // 2)
        roi = canvas_bgr[y0 : y0 + th, x0 : x0 + tw]
        roi[:] = alpha_blend_bgra_over_bgr(roi, resized)

    # Alias used by ``pigeon_0_8`` composite paths.
    render_on_bgr = render


__all__ = [
    "COLOR_ACCENT_DEFAULT",
    "COLOR_DESELECTED",
    "COLOR_INACTIVE",
    "COLOR_SELECTED",
    "COLOR_UI_DEFAULT",
    "DESIGN_H",
    "DESIGN_W",
    "KEYBOARD_NUMERIC_IP_SVG",
    "KEYBOARD_SVG_NAMES",
    "MainSettingsFocus",
    "MainSettingsState",
    "MainSettingsWidget",
    "SettingsTheme",
    "apply_main_settings_svg_state",
    "decode_svg_id",
    "default_main_settings_svg_path",
    "encode_svg_id",
    "keyboard_svg_path",
    "render_main_settings_bgra",
]
