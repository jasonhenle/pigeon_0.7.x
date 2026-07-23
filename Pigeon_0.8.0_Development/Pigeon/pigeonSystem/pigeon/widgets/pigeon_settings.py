"""
Pigeon device settings menu — ``settings_0.8/settings_pigeon.svg``.

Opened from main settings box1 (device panel). Five icon tabs + BACK.
Foreground art from ``settings_pigeon.svg``; diagonal stripe background uses
``menu_container_*`` geometry composited in code (all columns, static — not
focus-driven). The black canvas rect is hidden before rasterize.
"""

from __future__ import annotations

import copy
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np

from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.widgets.main_settings import (
    MainSettingsState,
    _composite_bgra_over_bgra,
    _ContainerStripeSpec,
    _discover_container_stripe_specs,
    _find_by_logical_id,
    _hex_to_bgr,
    _hide_container_stripe_rects,
    _composite_stroke_mask,
    _transform_rect_corners_svg,
    _prune_display_none,
    _rasterize_svg_tree,
    _set_text_content,
    _set_visible,
)

SVG_NS = "http://www.w3.org/2000/svg"

_PIGEON_VIEWBOX = (350.37, 441.08, 800.0, 480.0)

_PIGEON_FOCUS_RING: tuple[str, ...] = (
    "pigeon_back",
    "prefs_button",
    "color_button",
    "audio_button",
    "music_button",
    "update_button",
)

_PIGEON_BACKGROUND_CONTAINERS: tuple[str, ...] = tuple(
    f"menu_container_{i}" for i in range(1, 7)
)

_PIGEON_BACK_LAYERS: dict[str, tuple[str, ...]] = {
    "selected": (
        "_01_back_biutton_selected",
        "_01_button_back_text_selected",
    ),
    "deselected": (
        "_01_back_button_deselected",
        "_01_back_accent_deselected",
        "_01_button_back_text_deselected",
    ),
}

_PIGEON_MENU_LAYERS: dict[str, dict[str, object]] = {
    "prefs_button": {
        "title": "settings",
        "selected": ("_03_settingsicon_prefs_selected", "_03_button_prefs_selected"),
        "deselected": ("_03_settingicon_prefs_deselected",),
    },
    "color_button": {
        "title": "color",
        "selected": ("_04_settingsicon_color_selected", "_04_button_color_selected"),
        "deselected": (
            "_04_settingsicon_color_deselected",
            "_04_button_color_deselected",
        ),
    },
    "audio_button": {
        "title": "audio",
        "selected": ("_05_settingsicon_audio_selected", "_05_button_audio_selected"),
        "deselected": (
            "_05_settingsicon_audio_deselected",
            "_05_button_audio_deselected",
        ),
    },
    "music_button": {
        "title": "music",
        "selected": ("_06_settingsicon_music_selected", "_06_button_music_selected"),
        "deselected": (
            "_06_settingsicon_music_deselected",
            "_06_button_music_deselected",
        ),
    },
    "update_button": {
        "title": "update",
        "selected": (
            "_07_settingsicon_update_selected",
            "_07_button_text_update_selected",
        ),
        "deselected": (
            "_07_settingsicon_update_deselected",
            "_07_button_text_update_deselected",
        ),
    },
}

_HIDE_ALWAYS: tuple[str, ...] = ("_06_button_music_selected-2",)


def default_pigeon_settings_svg_path(assets_dir: Path | str | None = None) -> Path:
    env = os.environ.get("PIGEON_PIGEON_SETTINGS_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "settings_0.8" / "settings_pigeon.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "settings_0.8" / "settings_pigeon.svg"


def pigeon_focus_ring() -> tuple[str, ...]:
    return _PIGEON_FOCUS_RING


_PIGEON_SVG_TREE_TEMPLATES: dict[tuple[str, int, int], ET.Element] = {}
_PIGEON_SVG_TREE_TEMPLATE_MAX = 4


def _pigeon_svg_tree_from_path(path: Path) -> ET.Element:
    try:
        st = path.stat()
        key = (str(path.resolve()), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        key = (str(path), 0, 0)
    template = _PIGEON_SVG_TREE_TEMPLATES.get(key)
    if template is None:
        tree = ET.parse(path)
        root = tree.getroot()
        x, y, w, h = _PIGEON_VIEWBOX
        root.set("viewBox", f"{x} {y} {w} {h}")
        root.set("width", str(DESIGN_W))
        root.set("height", str(DESIGN_H))
        if len(_PIGEON_SVG_TREE_TEMPLATES) >= _PIGEON_SVG_TREE_TEMPLATE_MAX:
            _PIGEON_SVG_TREE_TEMPLATES.clear()
        _PIGEON_SVG_TREE_TEMPLATES[key] = root
        template = root
    return copy.deepcopy(template)


def _ensure_back_selected_text_contrast(root: ET.Element) -> None:
    """Selected BACK sits on a white pill; GFX export omits fill so text defaults to white."""
    back_text = _find_by_logical_id(root, "_01_button_back_text_selected")
    if back_text is None:
        return
    for node in back_text.iter():
        if node.tag.endswith("text") or node.tag.endswith("tspan"):
            node.set("fill", "#000")


def _ensure_update_deselected_layers(root: ET.Element) -> None:
    """Synthesize update deselected layers when missing from the GFX export."""
    if _find_by_logical_id(root, "_07_settingsicon_update_deselected") is not None:
        return
    sel_icon = _find_by_logical_id(root, "_07_settingsicon_update_selected")
    sel_btn = _find_by_logical_id(root, "_07_button_text_update_selected")
    if sel_icon is None:
        return

    def _parent_of(child: ET.Element) -> ET.Element | None:
        for parent in root.iter():
            if child in list(parent):
                return parent
        return None

    des_icon = copy.deepcopy(sel_icon)
    des_icon.set("id", "_07_settingsicon_update_deselected")
    for el in des_icon.iter():
        if el.tag.endswith("rect"):
            sw = str(el.get("stroke-width") or "")
            stroke = (el.get("stroke") or "").lower()
            if sw in ("12", "12.0") and stroke in ("#fff", "#ffffff", "white"):
                el.set("display", "none")
    parent = _parent_of(sel_icon)
    if parent is not None:
        parent.append(des_icon)

    if sel_btn is not None:
        des_btn = copy.deepcopy(sel_btn)
        des_btn.set("id", "_07_button_text_update_deselected")
        for rect in des_btn.iter():
            if not rect.tag.endswith("rect"):
                continue
            fill = (rect.get("fill") or "").lower()
            if fill in ("#fff", "#ffffff", "white"):
                rect.set("fill", "#202020")
                rect.set("stroke", "#000")
            elif fill in ("none", ""):
                stroke = (rect.get("stroke") or "").lower()
                if stroke in ("#000", "#000000", "#202020"):
                    rect.set("stroke", "#fff")
        for text in des_btn.iter():
            if text.tag.endswith("text") or text.tag.endswith("tspan"):
                text.set("fill", "#fff")
        parent_btn = _parent_of(sel_btn)
        if parent_btn is not None:
            parent_btn.append(des_btn)


def _set_heading_text(el: ET.Element | None, text: str) -> None:
    if el is None:
        return
    for node in el.iter():
        if not node.tag.endswith("text"):
            continue
        for child in list(node):
            node.remove(child)
        tspan = ET.SubElement(node, f"{{{SVG_NS}}}tspan")
        tspan.set("x", "0")
        tspan.set("y", "0")
        tspan.text = text


def _svg_to_pigeon_px(x_svg: float, y_svg: float) -> tuple[int, int]:
    vx, vy, vw, vh = _PIGEON_VIEWBOX
    x = int(round((x_svg - vx) * DESIGN_W / vw))
    y = int(round((y_svg - vy) * DESIGN_H / vh))
    return x, y


def _discover_pigeon_stripe_specs(root: ET.Element) -> tuple[_ContainerStripeSpec, ...]:
    specs: list[_ContainerStripeSpec] = []
    for cid in _PIGEON_BACKGROUND_CONTAINERS:
        specs.extend(_discover_container_stripe_specs(root, cid))
    return tuple(specs)


_PIGEON_MENU_RADIUS_PX = 20


def _pigeon_menu_container_mask() -> np.ndarray:
    """Full-panel rounded clip (pigeon is box1 zoomed to 800×480; black outside corners)."""
    from PIL import Image, ImageDraw

    mask = Image.new("L", (DESIGN_W, DESIGN_H), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, DESIGN_W - 1, DESIGN_H - 1),
        radius=_PIGEON_MENU_RADIUS_PX,
        fill=255,
    )
    return np.asarray(mask, dtype=np.uint8)


def _draw_pigeon_container_background_bgra(
    bgra: np.ndarray,
    stripes: tuple[_ContainerStripeSpec, ...],
) -> None:
    """Paint all pigeon menu stripe columns (static background, not focus-driven)."""
    if not stripes:
        return
    mask = _pigeon_menu_container_mask()
    for stripe in stripes:
        corners = _transform_rect_corners_svg(
            stripe.x_svg,
            stripe.y_svg,
            stripe.width_svg,
            stripe.height_svg,
            stripe.matrix,
        )
        pts = np.array([_svg_to_pigeon_px(x, y) for x, y in corners], dtype=np.int32)
        poly_mask = np.zeros(bgra.shape[:2], dtype=np.uint8)
        cv2.fillConvexPoly(poly_mask, pts, 255)
        poly_mask = cv2.bitwise_and(poly_mask, mask)
        _composite_stroke_mask(bgra, poly_mask, _hex_to_bgr(stripe.fill_hex))


def apply_pigeon_settings_svg_state(root: ET.Element, state: MainSettingsState) -> None:
    focused = state.pigeon_focused_id
    _ensure_back_selected_text_contrast(root)
    _ensure_update_deselected_layers(root)

    for lid in _HIDE_ALWAYS:
        _set_visible(_find_by_logical_id(root, lid), False)

    back_selected = focused == "pigeon_back"
    for lid in _PIGEON_BACK_LAYERS["selected"]:
        _set_visible(_find_by_logical_id(root, lid), back_selected)
    for lid in _PIGEON_BACK_LAYERS["deselected"]:
        _set_visible(_find_by_logical_id(root, lid), not back_selected)

    title = "update"
    for focus_id, spec in _PIGEON_MENU_LAYERS.items():
        selected = focused == focus_id
        if selected:
            title = str(spec["title"])
        for lid in spec["selected"]:  # type: ignore[union-attr]
            _set_visible(_find_by_logical_id(root, str(lid)), selected)
        for lid in spec["deselected"]:  # type: ignore[union-attr]
            _set_visible(_find_by_logical_id(root, str(lid)), not selected)

    _set_heading_text(_find_by_logical_id(root, "_02_settings_title_text"), title)
    ver = state.version_string
    if ver and not ver.startswith("v"):
        ver = f"v.{ver}"
    _set_text_content(_find_by_logical_id(root, "pigeon_version_text"), ver)


def render_pigeon_settings_bgra(
    state: MainSettingsState | None = None,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    if svg_path is not None:
        path = Path(svg_path)
    else:
        path = default_pigeon_settings_svg_path(assets_dir)
    if not path.is_file():
        raise FileNotFoundError(f"pigeon settings SVG not found: {path}")

    st = state if state is not None else MainSettingsState()
    root = _pigeon_svg_tree_from_path(path)
    apply_pigeon_settings_svg_state(root, st)
    stripe_specs = _discover_pigeon_stripe_specs(root)
    _hide_container_stripe_rects(root, _PIGEON_BACKGROUND_CONTAINERS)
    bg = _find_by_logical_id(root, "background")
    if bg is not None:
        _set_visible(bg, False)
    _prune_display_none(root)
    ui_bgra = _rasterize_svg_tree(root)
    bg_bgra = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
    bg_bgra[:, :, :3] = 0
    bg_bgra[:, :, 3] = 255
    _draw_pigeon_container_background_bgra(bg_bgra, stripe_specs)
    return _composite_bgra_over_bgra(bg_bgra, ui_bgra)


__all__ = [
    "apply_pigeon_settings_svg_state",
    "default_pigeon_settings_svg_path",
    "pigeon_focus_ring",
    "render_pigeon_settings_bgra",
]
