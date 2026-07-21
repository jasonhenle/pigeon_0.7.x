"""
Pigeon 0.8 settings keyboards — SVG overlays shared by main_settings text entry.

Layouts (``pigeonAssets/settings_0.8/``):
  - keyboard_qwerty_lower / keyboard_qwerty_upper
  - keyboard_symbolic
  - keyboard_numeric_all  (ids: keyboard_numeric_full_*)
  - keyboard_numeric_pin
  - keyboard_bottom_row   (shared ABC / abc / sym / space / delete / cancel / go)

Navigation is linear Left/Right. Physical Spacebar activates the focused key.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import cv2
import numpy as np

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.widgets.main_settings import (
    DESIGN_H,
    DESIGN_W,
    SettingsTheme,
    _BUTTON_FILL_CANDIDATES,
    _apply_button_fill,
    _apply_contrast_paint,
    _find_by_logical_id,
    _normalize_logical,
    _parent_map,
    _prune_display_none,
    _rewrite_style_prop,
    _set_paint,
    _set_text_content,
    _set_visible,
    keyboard_svg_path,
)
from pigeon.widgets.settings_svg_text import rasterize_settings_svg_bgra, viewbox_from_root

# Illustrator keyboard buttons ship as near-black, not theme deselected.
_KB_BUTTON_EXTRA = frozenset({"#231f20", "#231f20ff"})
_KB_STROKE_WIDTH = "3"


class KeyboardMode(str, Enum):
    QWERTY_LOWER = "qwerty_lower"
    QWERTY_UPPER = "qwerty_upper"
    SYMBOLIC = "symbolic"
    NUMERIC_ALL = "numeric_all"
    NUMERIC_PIN = "numeric_pin"


class KeyAction(str, Enum):
    CHAR = "char"
    SPACE = "space"
    DELETE = "delete"
    SHIFT = "shift"
    MODE_ABC = "mode_abc"  # uppercase
    MODE_ABC_LOWER = "mode_abc_lower"
    MODE_SYM = "mode_sym"
    MODE_123 = "mode_123"
    CANCEL = "cancel"
    GO = "go"


@dataclass(frozen=True)
class KeySpec:
    """One focusable key: button layer + optional contrast icon layers."""

    button_id: str
    action: KeyAction
    char: str = ""
    icon_ids: tuple[str, ...] = ()


_MODE_SVG: dict[KeyboardMode, str] = {
    KeyboardMode.QWERTY_LOWER: "keyboard_qwerty_lower.svg",
    KeyboardMode.QWERTY_UPPER: "keyboard_qwerty_upper.svg",
    KeyboardMode.SYMBOLIC: "keyboard_symbolic.svg",
    KeyboardMode.NUMERIC_ALL: "keyboard_numeric_all.svg",
    KeyboardMode.NUMERIC_PIN: "keyboard_numeric_pin.svg",
}

_BOTTOM_ROW_SVG = "keyboard_bottom_row.svg"

# Bottom-row strip on the 800×480 artboard.
_BOTTOM_ROW_X = 37  # (800 - 725) / 2
_BOTTOM_ROW_Y = 378

# Linear focus order — mode keys differ for uppercase-only fields (Digital-7).
_BOTTOM_ROW_TAIL: tuple[KeySpec, ...] = (
    KeySpec(
        "keyboard_qwerty_space_button",
        KeyAction.SPACE,
        char=" ",
    ),
    KeySpec(
        "keyboard_bottom_row_delete_button",
        KeyAction.DELETE,
        icon_ids=("keyboard_qwerty_upper_delete_icon",),
    ),
    KeySpec(
        "keyboard_qwerty_cancel_button",
        KeyAction.CANCEL,
        icon_ids=("keyboard_qwerty_cancel_icon",),
    ),
    KeySpec(
        "keyboard_qwerty_go_button",
        KeyAction.GO,
        icon_ids=("keyboard_qwerty_go_icon",),
    ),
)

_BOTTOM_ROW_NETWORK: tuple[KeySpec, ...] = (
    KeySpec(
        "keyboard_bottom_row_button1_buton",
        KeyAction.MODE_ABC,
        icon_ids=("keyboard_bottom_row_button1_ABC_icon-2",),
    ),
    KeySpec(
        "keyboard_bottom_row_button2_button",
        KeyAction.MODE_123,
        icon_ids=("keyboard_bottom_row_button2_abc",),
    ),
    KeySpec(
        "keyboard_bottom_row_button3_button",
        KeyAction.MODE_SYM,
        icon_ids=("keyboard_bottom_row_button3_sym_icon",),
    ),
) + _BOTTOM_ROW_TAIL

# Back-compat alias used in tests / exports.
_BOTTOM_ROW_KEYS = _BOTTOM_ROW_NETWORK

# Uppercase-only: no button1; button2 → numeric, button3 → symbolic.
_BOTTOM_ROW_UPPERCASE: tuple[KeySpec, ...] = (
    KeySpec(
        "keyboard_bottom_row_button2_button",
        KeyAction.MODE_123,
        icon_ids=("keyboard_bottom_row_button2_abc",),
    ),
    KeySpec(
        "keyboard_bottom_row_button3_button",
        KeyAction.MODE_SYM,
        icon_ids=("keyboard_bottom_row_button3_sym_icon",),
    ),
) + _BOTTOM_ROW_TAIL


@dataclass
class KeyboardState:
    """Live text-entry keyboard overlay state."""

    mode: KeyboardMode = KeyboardMode.QWERTY_LOWER
    focus_index: int = 0
    buffer: str = ""
    initial_text: str = ""
    target: str = ""  # e.g. "location", "network", "pin"
    theme: SettingsTheme = field(default_factory=SettingsTheme)
    supports_lowercase: bool = True
    password_mask: bool = False
    # Cached focus ring for the active mode (char keys + bottom row).
    focus_ring: tuple[KeySpec, ...] = field(default_factory=tuple)
    include_bottom_row: bool = True

    def rebuild_focus_ring(self, *, assets_dir: Path | str | None = None) -> None:
        char_keys = discover_char_keys(self.mode, assets_dir=assets_dir)
        bottom = _BOTTOM_ROW_NETWORK if self.supports_lowercase else _BOTTOM_ROW_UPPERCASE
        if self.include_bottom_row and self.mode != KeyboardMode.NUMERIC_PIN:
            self.focus_ring = tuple(char_keys) + bottom
        elif self.mode == KeyboardMode.NUMERIC_PIN:
            pin_bottom = tuple(
                k
                for k in _BOTTOM_ROW_TAIL
                if k.action in (KeyAction.DELETE, KeyAction.CANCEL, KeyAction.GO)
            )
            self.focus_ring = tuple(char_keys) + pin_bottom
            self.include_bottom_row = True
        else:
            self.focus_ring = tuple(char_keys)
        if not self.focus_ring:
            self.focus_ring = bottom
        self.focus_index = int(self.focus_index) % len(self.focus_ring)

    @property
    def focused(self) -> KeySpec:
        if not self.focus_ring:
            self.rebuild_focus_ring()
        return self.focus_ring[int(self.focus_index) % len(self.focus_ring)]

    def navigate(self, *, forward: bool = True) -> None:
        if not self.focus_ring:
            self.rebuild_focus_ring()
        n = len(self.focus_ring)
        step = 1 if forward else -1
        self.focus_index = (int(self.focus_index) + step) % n

    def set_mode(self, mode: KeyboardMode, *, assets_dir: Path | str | None = None) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        self.focus_index = 0
        self.rebuild_focus_ring(assets_dir=assets_dir)


def _button_xy(el: ET.Element) -> tuple[float, float]:
    """Sort key: top→bottom, left→right."""
    x = el.get("x")
    y = el.get("y")
    if x is not None and y is not None:
        try:
            return float(x), float(y)
        except ValueError:
            pass
    d = el.get("d") or ""
    m = re.search(r"[Mm]\s*([-\d.]+)[,\s]+([-\d.]+)", d)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    return (0.0, 0.0)


def _pair_icon_id(button_id: str) -> str:
    """Best-effort button → icon id (handles known typos in exports)."""
    if button_id.endswith("_button"):
        base = button_id[: -len("_button")]
        return f"{base}_icon"
    if button_id.endswith("_buton"):  # bottom-row typo
        return button_id.replace("_buton", "_icon")
    return button_id + "_icon"


def discover_char_keys(
    mode: KeyboardMode,
    *,
    assets_dir: Path | str | None = None,
) -> list[KeySpec]:
    """Build character-key specs from the mode SVG (sorted by position)."""
    path = keyboard_svg_path(_MODE_SVG[mode], assets_dir=assets_dir)
    if not path.is_file():
        return []
    root = ET.parse(path).getroot()

    # Index icon layers by normalized id for fuzzy pairing.
    icon_nodes: dict[str, ET.Element] = {}
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        logical = _normalize_logical(raw)
        if logical.endswith("_icon") or "_icon_" in logical or logical.endswith("_ico") or logical.endswith("_ico_n"):
            icon_nodes[logical] = el

    buttons: list[tuple[float, float, str, ET.Element]] = []
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        logical = _normalize_logical(raw)
        is_button = (
            logical.endswith("_button")
            or logical.endswith("_buton")
            or logical.startswith("symbolic_button_")
        )
        if not is_button:
            continue
        # Skip any bottom-row ids if they ever appear inside a char SVG.
        if "bottom_row" in logical or logical.startswith("keyboard_qwerty_space"):
            continue
        if logical.endswith(("_go_button", "_cancel_button", "_delete_button")):
            continue
        x, y = _button_xy(el)
        buttons.append((y, x, logical, el))

    buttons.sort(key=lambda t: (round(t[0], 1), round(t[1], 1)))
    keys: list[KeySpec] = []
    for _y, _x, logical, _el in buttons:
        if "shift" in logical.lower():
            shift_icons = (
                ("keyboard_qwerty_lower_shift_icon",)
                if mode == KeyboardMode.QWERTY_LOWER
                else ("keyboard_qwerty_upper_SHIFT_icon", "keyboard_qwerty_upper_SHIFT_icon-2")
            )
            keys.append(KeySpec(logical, KeyAction.SHIFT, icon_ids=shift_icons))
            continue

        # Symbolic: symbolic_button_X
        if logical.startswith("symbolic_button_"):
            ch = logical[len("symbolic_button_") :]
            icon_id = f"symbolic_icon_{ch}"
            # Prefer an existing icon node (may carry AI uniqueness suffix).
            resolved = icon_id
            for cand, node in icon_nodes.items():
                if cand == icon_id or cand.startswith(icon_id + "_"):
                    resolved = cand
                    break
            keys.append(KeySpec(logical, KeyAction.CHAR, char=ch, icon_ids=(resolved,)))
            continue

        icon_candidates = [_pair_icon_id(logical)]
        # Export quirks.
        if logical.startswith("_"):
            # e.g. ``_keyboard_qwerty_lower_b_button``
            icon_candidates.insert(0, _pair_icon_id(logical.lstrip("_")))
        if logical == "keyboard_qwerty_q_button":
            icon_candidates = ["keyboard_qwerty_lower_q_icon", "keyboard_qwerty_q_icon"]
        if mode == KeyboardMode.QWERTY_UPPER and logical.endswith("_button"):
            # Icons use uppercase letter: …_Q_icon not …_q_icon
            base = logical[: -len("_button")]
            # keyboard_qwerty_upper_q → keyboard_qwerty_upper_Q_icon
            parts = base.rsplit("_", 1)
            if len(parts) == 2 and len(parts[1]) == 1:
                icon_candidates.insert(0, f"{parts[0]}_{parts[1].upper()}_icon")
        if logical == "keyboard_numeric_full__button":
            icon_candidates = ["keyboard_numeric_full_1_icon", "keyboard_numeric_full__icon"]
        if logical.endswith("_2_button"):
            icon_candidates.append(logical.replace("_button", "_ico"))
        if logical.endswith("_3_button"):
            icon_candidates.append(logical.replace("_button", "_ico_n"))

        ch = ""
        resolved_icon = icon_candidates[0]
        for cand in icon_candidates:
            # Exact or prefix match against indexed icons.
            hit = icon_nodes.get(cand)
            if hit is None:
                for iid, node in icon_nodes.items():
                    if iid == cand or iid.startswith(cand + "_"):
                        hit = node
                        cand = iid
                        break
            if hit is not None:
                text = "".join(hit.itertext()).strip()
                if text:
                    ch = text
                    resolved_icon = cand
                    break
                # Uppercase icons may be path glyphs with empty text — derive from id.
                m = re.search(r"_([A-Za-z0-9])_icon", cand)
                if m:
                    ch = m.group(1)
                    resolved_icon = cand
                    break

        # Last resort: single letter from button id (…_b_button / …_B_button).
        if not ch:
            m = re.search(r"_([A-Za-z0-9])_button$", logical.lstrip("_"))
            if m:
                ch = m.group(1)

        if not ch and mode in (KeyboardMode.NUMERIC_ALL, KeyboardMode.NUMERIC_PIN):
            m = re.search(r"_(\d)_button$", logical)
            if m:
                ch = m.group(1)
            elif "full__button" in logical:
                ch = "1"
            elif logical.endswith("_0_button"):
                ch = "0"

        if mode == KeyboardMode.QWERTY_UPPER and ch:
            ch = ch.upper()
        elif mode == KeyboardMode.QWERTY_LOWER and ch:
            ch = ch.lower()

        keys.append(
            KeySpec(
                logical,
                KeyAction.CHAR,
                char=ch,
                icon_ids=(resolved_icon,),
            )
        )
    return keys


def _paint_kb_button_shape(
    node: ET.Element,
    *,
    selected: bool,
    theme: SettingsTheme,
) -> None:
    """Fill + 3px outline (white deselected, black selected)."""
    fill = theme.selected if selected else theme.deselected
    stroke = theme.deselected if selected else theme.selected
    _set_paint(node, fill=fill, stroke=stroke)
    style = node.get("style") or ""
    style = _rewrite_style_prop(style, "stroke-width", _KB_STROKE_WIDTH)
    node.set("stroke-width", _KB_STROKE_WIDTH)
    if style:
        node.set("style", style)


def apply_keyboard_selection(
    root: ET.Element,
    *,
    focused_button_id: str,
    theme: SettingsTheme,
    button_ids: set[str],
    icon_ids_by_button: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Recolor every known button; contrast paint on paired icons/text."""
    from pigeon.widgets.main_settings import _iter_style_fill_stroke, _set_paint

    icon_map = icon_ids_by_button or {}
    fill_ok = set(_BUTTON_FILL_CANDIDATES) | _KB_BUTTON_EXTRA | {
        theme.selected.lower(),
        theme.deselected.lower(),
        "#ffffff",
        "#fff",
    }

    # Parent lookup for sibling icon contrast (PIN / numeric groups).
    parents: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parents[child] = parent

    for logical in button_ids:
        selected = logical == focused_button_id
        el = _find_by_logical_id(root, logical)
        if el is None:
            continue
        fill = theme.selected if selected else theme.deselected
        for node in el.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in ("path", "rect", "polygon", "circle", "ellipse"):
                continue
            nid = _normalize_logical(node.get("id") or "")
            if nid.endswith("_accent"):
                continue
            cur_fill, _ = _iter_style_fill_stroke(node)
            if cur_fill in ("none", "transparent"):
                continue
            if cur_fill is None or cur_fill in fill_ok:
                _paint_kb_button_shape(node, selected=selected, theme=theme)
        _apply_button_fill(el, selected=selected, theme=theme)

        icons = list(icon_map.get(logical, ()))
        paired_icon = _pair_icon_id(logical)
        icons.append(paired_icon)
        seen_icons: set[str] = set()
        for icon_logical in icons:
            if icon_logical in seen_icons:
                continue
            seen_icons.add(icon_logical)
            icon_el = _find_by_logical_id(root, icon_logical)
            if icon_el is not None:
                _apply_contrast_paint(icon_el, selected=selected, theme=theme)

        # Grouped layouts (PIN / numeric): icon is a sibling under the same parent.
        expected_icons = seen_icons
        parent = parents.get(el)
        if parent is not None:
            for child in parent:
                cid = _normalize_logical(child.get("id") or "")
                if cid in expected_icons:
                    _apply_contrast_paint(child, selected=selected, theme=theme)


def _bottom_row_icons(root: ET.Element, group_logical: str, *icon_logicals: str) -> list[ET.Element]:
    """Return icon/text nodes under one bottom-row button group."""
    group = _find_by_logical_id(root, group_logical)
    if group is None:
        return []
    want = {_normalize_logical(name) for name in icon_logicals}
    hits: list[ET.Element] = []
    for el in group.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        if _normalize_logical(raw) in want:
            hits.append(el)
    return hits


# Pill-button centers on the bottom-row artboard (725×42 SVG units).
_BOTTOM_ROW_BTN1_CX = 22.25
_BOTTOM_ROW_BTN2_CX = 88.55
_BOTTOM_ROW_BTN3_CX = 164.25
_BOTTOM_ROW_LABEL_BASELINE = 28.73


def _layout_bottom_row_label(
    el: ET.Element | None,
    text: str,
    *,
    center_x: float,
    font_size: str = "20",
    baseline_y: float = _BOTTOM_ROW_LABEL_BASELINE,
) -> None:
    """Center a bottom-row mode label on its pill and normalize font metrics."""
    if el is None:
        return
    el.set("font-size", font_size)
    el.set("text-anchor", "middle")
    el.set("transform", f"translate({center_x:.2f} {baseline_y:.2f})")
    _set_text_content(el, text)
    for tspan in el.iter():
        if tspan.tag.endswith("tspan"):
            tspan.set("x", "0")
            tspan.set("y", "0")


def _remove_bottom_row_button1(root: ET.Element) -> None:
    """Drop button1 from the SVG tree (PyMuPDF ignores ``display:none``)."""
    btn1 = _find_by_logical_id(root, "keyboard_bottom_row_button1")
    if btn1 is None:
        return
    parents = _parent_map(root)
    parent = parents.get(btn1)
    if parent is not None:
        parent.remove(btn1)


def apply_bottom_row_mode_icons(
    root: ET.Element,
    mode: KeyboardMode,
    *,
    uppercase_only: bool = False,
) -> None:
    """Show one mode label per bottom-row button (never two labels on the same key)."""
    btn1_group = _find_by_logical_id(root, "keyboard_bottom_row_button1")
    btn1_abc = _bottom_row_icons(
        root,
        "keyboard_bottom_row_button1",
        "keyboard_bottom_row_button1_ABC_icon-2",
        "keyboard_bottom_row_button1_ABC_icon",
    )
    btn1_123 = _bottom_row_icons(root, "keyboard_bottom_row_button1", "keyboard_bottom_row_button1_123_icon")
    btn2_abc = _bottom_row_icons(root, "keyboard2", "keyboard_bottom_row_button2_abc")
    btn2_abc_dup = _bottom_row_icons(root, "keyboard2", "keyboard_bottom_row_button1_ABC_icon")
    btn3_sym = _bottom_row_icons(root, "keyboard3", "keyboard_bottom_row_button3_sym_icon")
    btn3_123 = _bottom_row_icons(root, "keyboard3", "keyboard_bottom_row_button3_123_icon")

    for el in btn1_abc + btn1_123 + btn2_abc + btn2_abc_dup + btn3_sym + btn3_123:
        _set_visible(el, False)

    if uppercase_only:
        _remove_bottom_row_button1(root)
        _layout_bottom_row_label(
            _find_by_logical_id(root, "keyboard_bottom_row_button2_abc"),
            "123",
            center_x=_BOTTOM_ROW_BTN2_CX,
        )
        for el in btn2_abc:
            _set_visible(el, True)
        _layout_bottom_row_label(
            _find_by_logical_id(root, "keyboard_bottom_row_button3_sym_icon"),
            "sym",
            center_x=_BOTTOM_ROW_BTN3_CX,
            font_size="25",
            baseline_y=28.01,
        )
        for el in btn3_sym:
            _set_visible(el, True)
        return

    _set_visible(btn1_group, True)

    for el in btn1_123 + btn2_abc_dup + btn3_123:
        _set_visible(el, False)

    _layout_bottom_row_label(
        _find_by_logical_id(root, "keyboard_bottom_row_button2_abc"),
        "123",
        center_x=_BOTTOM_ROW_BTN2_CX,
    )
    _layout_bottom_row_label(
        _find_by_logical_id(root, "keyboard_bottom_row_button3_sym_icon"),
        "sym",
        center_x=_BOTTOM_ROW_BTN3_CX,
        font_size="25",
        baseline_y=28.01,
    )

    for el in btn2_abc:
        _set_visible(el, True)
    for el in btn3_sym:
        _set_visible(el, True)

    if mode == KeyboardMode.QWERTY_LOWER:
        case_label = "ABC"
    elif mode == KeyboardMode.QWERTY_UPPER:
        case_label = "abc"
    else:
        case_label = "ABC"

    btn1_label = _find_by_logical_id(root, "keyboard_bottom_row_button1_ABC_icon-2")
    if btn1_label is None:
        btn1_label = _find_by_logical_id(root, "keyboard_bottom_row_button1_ABC_icon")
    _layout_bottom_row_label(
        btn1_label,
        case_label,
        center_x=_BOTTOM_ROW_BTN1_CX,
        font_size="22",
        baseline_y=28.73,
    )
    for el in btn1_abc[:1] or btn1_abc:
        _set_visible(el, True)


def _fit_full_artboard(root: ET.Element) -> None:
    """Match main_settings: native 800×480 artboard."""
    root.set("viewBox", "0 0 800 480")
    root.set("width", str(DESIGN_W))
    root.set("height", str(DESIGN_H))


def _rasterize_keyboard_chars(
    state: KeyboardState,
    *,
    assets_dir: Path | str | None,
) -> np.ndarray:
    path = keyboard_svg_path(_MODE_SVG[state.mode], assets_dir=assets_dir)
    if not path.is_file():
        return np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)

    root = ET.parse(path).getroot()
    button_ids: set[str] = set()
    icon_map: dict[str, tuple[str, ...]] = {}
    for k in state.focus_ring:
        if k.action in (KeyAction.CHAR, KeyAction.SHIFT):
            button_ids.add(k.button_id)
            if k.icon_ids:
                icon_map[k.button_id] = k.icon_ids

    focused = state.focused.button_id
    char_focus = focused if focused in button_ids else ""
    apply_keyboard_selection(
        root,
        focused_button_id=char_focus,
        theme=state.theme,
        button_ids=button_ids,
        icon_ids_by_button=icon_map,
    )

    if state.mode == KeyboardMode.NUMERIC_PIN:
        vb = viewbox_from_root(root)
        target_w = 320
        target_h = max(1, int(round(vb[3] * target_w / max(vb[2], 1.0))))
        pad = rasterize_settings_svg_bgra(
            root,
            width=target_w,
            height=target_h,
            view_box=vb,
            font_mode="keyboard",
        )
        canvas = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
        y0 = max(0, (DESIGN_H - pad.shape[0]) // 2)
        x0 = max(0, (DESIGN_W - pad.shape[1]) // 2)
        y1 = min(DESIGN_H, y0 + pad.shape[0])
        x1 = min(DESIGN_W, x0 + pad.shape[1])
        canvas[y0:y1, x0:x1] = pad[: y1 - y0, : x1 - x0]
        return canvas

    _fit_full_artboard(root)
    return rasterize_settings_svg_bgra(
        root, width=DESIGN_W, height=DESIGN_H, font_mode="keyboard"
    )


def _rasterize_bottom_row(
    state: KeyboardState,
    *,
    assets_dir: Path | str | None,
) -> np.ndarray | None:
    if not state.include_bottom_row:
        return None
    path = keyboard_svg_path(_BOTTOM_ROW_SVG, assets_dir=assets_dir)
    if not path.is_file():
        return None
    root = ET.parse(path).getroot()
    apply_bottom_row_mode_icons(root, state.mode, uppercase_only=not state.supports_lowercase)

    bottom = _BOTTOM_ROW_NETWORK if state.supports_lowercase else _BOTTOM_ROW_UPPERCASE
    button_ids = {k.button_id for k in bottom}
    icon_map = {k.button_id: k.icon_ids for k in bottom if k.icon_ids}
    focused = state.focused.button_id
    row_focus = focused if focused in button_ids else ""
    apply_keyboard_selection(
        root,
        focused_button_id=row_focus,
        theme=state.theme,
        button_ids=button_ids,
        icon_ids_by_button=icon_map,
    )
    _prune_display_none(root)

    vb = viewbox_from_root(root)
    target_w = int(round(725 * (DESIGN_W / 800)))
    target_h = max(1, int(round(vb[3] * target_w / max(vb[2], 1.0))))
    return rasterize_settings_svg_bgra(
        root, width=target_w, height=target_h, view_box=vb, font_mode="keyboard"
    )


def render_keyboard_bgra(
    state: KeyboardState,
    *,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    """Composite character keys + bottom row → 800×480 BGRA."""
    if not state.focus_ring:
        state.rebuild_focus_ring(assets_dir=assets_dir)

    canvas = _rasterize_keyboard_chars(state, assets_dir=assets_dir)
    row = _rasterize_bottom_row(state, assets_dir=assets_dir)
    if row is not None and row.size:
        x0 = int(_BOTTOM_ROW_X * (DESIGN_W / 800))
        y0 = int(_BOTTOM_ROW_Y * (DESIGN_H / 480))
        # PIN: park bottom actions under the centered pad.
        if state.mode == KeyboardMode.NUMERIC_PIN:
            y0 = min(DESIGN_H - row.shape[0] - 8, DESIGN_H - 60)
            x0 = max(0, (DESIGN_W - row.shape[1]) // 2)
        y1 = min(DESIGN_H, y0 + row.shape[0])
        x1 = min(DESIGN_W, x0 + row.shape[1])
        rh, rw = y1 - y0, x1 - x0
        if rh > 0 and rw > 0:
            region = canvas[y0:y1, x0:x1]
            strip = row[:rh, :rw]
            base_bgr = region[:, :, :3]
            blended = alpha_blend_bgra_over_bgr(base_bgr, strip)
            alpha = strip[:, :, 3:4].astype(np.float32) / 255.0
            out_a = np.clip(
                alpha * 255.0 + (1.0 - alpha) * region[:, :, 3:4].astype(np.float32),
                0,
                255,
            ).astype(np.uint8)
            canvas[y0:y1, x0:x1, :3] = blended
            canvas[y0:y1, x0:x1, 3:4] = out_a
    return canvas


def activate_key(state: KeyboardState, *, assets_dir: Path | str | None = None) -> str:
    """
    Apply the focused key. Returns an action token:
      - ``typing`` — buffer changed
      - ``cancel`` — discard / close
      - ``go`` — commit buffer / close
      - ``mode:<name>`` — layout switched
    """
    if not state.focus_ring:
        state.rebuild_focus_ring(assets_dir=assets_dir)
    key = state.focused
    act = key.action

    if act == KeyAction.CHAR:
        if key.char:
            ch = key.char
            if not state.supports_lowercase:
                ch = ch.upper()
            state.buffer += ch
        return "typing"
    if act == KeyAction.SPACE:
        state.buffer += " "
        return "typing"
    if act == KeyAction.DELETE:
        state.buffer = state.buffer[:-1]
        return "typing"
    if act == KeyAction.SHIFT:
        if not state.supports_lowercase:
            return "typing"
        if state.mode == KeyboardMode.QWERTY_LOWER:
            state.set_mode(KeyboardMode.QWERTY_UPPER, assets_dir=assets_dir)
        else:
            state.set_mode(KeyboardMode.QWERTY_LOWER, assets_dir=assets_dir)
        return f"mode:{state.mode.value}"
    if act == KeyAction.MODE_ABC:
        if state.supports_lowercase:
            if state.mode == KeyboardMode.QWERTY_LOWER:
                state.set_mode(KeyboardMode.QWERTY_UPPER, assets_dir=assets_dir)
            elif state.mode == KeyboardMode.QWERTY_UPPER:
                state.set_mode(KeyboardMode.QWERTY_LOWER, assets_dir=assets_dir)
            else:
                state.set_mode(KeyboardMode.QWERTY_LOWER, assets_dir=assets_dir)
        else:
            state.set_mode(KeyboardMode.QWERTY_UPPER, assets_dir=assets_dir)
        return f"mode:{state.mode.value}"
    if act == KeyAction.MODE_ABC_LOWER:
        state.set_mode(KeyboardMode.QWERTY_LOWER, assets_dir=assets_dir)
        return f"mode:{state.mode.value}"
    if act == KeyAction.MODE_SYM:
        if not state.supports_lowercase:
            if state.mode == KeyboardMode.SYMBOLIC:
                state.set_mode(KeyboardMode.QWERTY_UPPER, assets_dir=assets_dir)
            else:
                state.set_mode(KeyboardMode.SYMBOLIC, assets_dir=assets_dir)
        elif state.mode == KeyboardMode.SYMBOLIC:
            state.set_mode(KeyboardMode.QWERTY_LOWER, assets_dir=assets_dir)
        else:
            state.set_mode(KeyboardMode.SYMBOLIC, assets_dir=assets_dir)
        return f"mode:{state.mode.value}"
    if act == KeyAction.MODE_123:
        if not state.supports_lowercase:
            if state.mode == KeyboardMode.NUMERIC_ALL:
                state.set_mode(KeyboardMode.QWERTY_UPPER, assets_dir=assets_dir)
            else:
                state.set_mode(KeyboardMode.NUMERIC_ALL, assets_dir=assets_dir)
        elif state.mode == KeyboardMode.NUMERIC_ALL:
            state.set_mode(KeyboardMode.QWERTY_LOWER, assets_dir=assets_dir)
        else:
            state.set_mode(KeyboardMode.NUMERIC_ALL, assets_dir=assets_dir)
        return f"mode:{state.mode.value}"
    if act == KeyAction.CANCEL:
        state.buffer = state.initial_text
        return "cancel"
    if act == KeyAction.GO:
        return "go"
    return "typing"


def focus_first_letter(state: KeyboardState, *, assets_dir: Path | str | None = None) -> None:
    """Move focus to the top-left character key (``q`` / ``Q`` on QWERTY layouts)."""
    if not state.focus_ring:
        state.rebuild_focus_ring(assets_dir=assets_dir)
    want = "q" if state.supports_lowercase else "Q"
    for i, key in enumerate(state.focus_ring):
        if key.action == KeyAction.CHAR and key.char.lower() == want.lower():
            state.focus_index = i
            return
    for i, key in enumerate(state.focus_ring):
        if key.action == KeyAction.CHAR:
            state.focus_index = i
            return


def open_keyboard(
    *,
    target: str,
    initial_text: str = "",
    mode: KeyboardMode | None = None,
    theme: SettingsTheme | None = None,
    assets_dir: Path | str | None = None,
) -> KeyboardState:
    """Create a ready-to-navigate keyboard state for a text field."""
    supports_lower = target == "network"
    password_mask = target == "network"
    if mode is None:
        if target == "pin":
            mode = KeyboardMode.NUMERIC_PIN
        elif target == "location":
            mode = KeyboardMode.QWERTY_UPPER
        else:
            mode = KeyboardMode.QWERTY_LOWER
    st = KeyboardState(
        mode=mode,
        buffer="",
        initial_text=initial_text,
        target=target,
        theme=theme if theme is not None else SettingsTheme(),
        supports_lowercase=supports_lower,
        password_mask=password_mask,
    )
    st.rebuild_focus_ring(assets_dir=assets_dir)
    focus_first_letter(st, assets_dir=assets_dir)
    return st


__all__ = [
    "KeyAction",
    "KeySpec",
    "KeyboardMode",
    "KeyboardState",
    "activate_key",
    "discover_char_keys",
    "focus_first_letter",
    "open_keyboard",
    "render_keyboard_bgra",
]
