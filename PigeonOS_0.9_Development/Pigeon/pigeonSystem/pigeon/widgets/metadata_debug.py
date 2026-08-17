"""
Metadata inspector — ``settings_0.8/pigeon_metadata.svg``.

Three stacked variants of one screen (player / hdmi / pigeon) that show, per
source, what Pigeon knows about the current title. Opened with [4]; forward /
backward steps player → hdmi → pigeon (no wrap); EXIT returns to the pigeon
settings grid; [4] again jumps to the legacy metadata view.

Data comes from a provider callback registered by ``pigeon_0_9`` (the widget
layer has no access to pyatv / OCR state).
"""

from __future__ import annotations

import copy
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.widgets.main_settings import (
    MainSettingsState,
    _composite_bgra_over_bgra,
    _disable_embedded_settings_background_layers,
    _find_by_logical_id,
    _prune_display_none,
    _set_paint,
    _set_visible,
)

# Same Illustrator board crop as settings_pigeon.svg so the panel lines up
# with the shared code-drawn theme background.
_METADATA_VIEWBOX = (363.7, 441.8, 800.0, 480.0)

_COLOR_STATUS_OK = "#0DFF00"
_COLOR_STATUS_BAD = "#FF0013"

# Page order for forward/backward stepping.
METADATA_DEBUG_PAGES: tuple[str, ...] = ("player", "hdmi", "pigeon")

# Row order matches the five field labels in the SVG (top → bottom).
_ROW_KEYS: tuple[str, ...] = ("service", "series", "title", "episode", "year")

_ROW_TEXT_MAX_CHARS = 26

# (path, mtime_ns) → parsed template; deepcopy on return (callers mutate).
_SVG_TREE_TEMPLATES: dict[tuple[str, int], ET.Element] = {}
_SVG_TREE_TEMPLATE_MAX = 2

_THEME_BG_CACHE: dict[tuple[object, ...], np.ndarray] = {}
_THEME_BG_CACHE_MAX = 4

# Registered by pigeon_0_9; returns the live per-source rows + active flags.
_DATA_PROVIDER: Callable[[], dict[str, Any]] | None = None


def set_metadata_debug_data_provider(fn: Callable[[], dict[str, Any]] | None) -> None:
    global _DATA_PROVIDER
    _DATA_PROVIDER = fn


def metadata_debug_data() -> dict[str, Any]:
    """Snapshot from the registered provider (empty when unavailable)."""
    fn = _DATA_PROVIDER
    if fn is None:
        return {}
    try:
        data = fn()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def default_metadata_debug_svg_path(assets_dir: Path | str | None = None) -> Path:
    env = os.environ.get("PIGEON_METADATA_DEBUG_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "settings_0.8" / "pigeon_metadata.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "settings_0.8" / "pigeon_metadata.svg"


def _svg_tree_from_path(path: Path) -> ET.Element:
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    template = _SVG_TREE_TEMPLATES.get(key)
    if template is None:
        tree = ET.parse(path)
        root = tree.getroot()
        x, y, w, h = _METADATA_VIEWBOX
        root.set("viewBox", f"{x} {y} {w} {h}")
        root.set("width", str(DESIGN_W))
        root.set("height", str(DESIGN_H))
        if len(_SVG_TREE_TEMPLATES) >= _SVG_TREE_TEMPLATE_MAX:
            _SVG_TREE_TEMPLATES.clear()
        _SVG_TREE_TEMPLATES[key] = root
        template = root
    return copy.deepcopy(template)


def _group_texts(root: ET.Element, logical_id: str) -> list[ET.Element]:
    """Direct <text> children of a group, in document (top → bottom) order."""
    group = _find_by_logical_id(root, logical_id)
    if group is None:
        return []
    return [el for el in group if el.tag.endswith("text")]


def _set_flat_text(text_el: ET.Element | None, value: str) -> None:
    """Replace an Illustrator per-glyph-tspan text with one flat string."""
    if text_el is None:
        return
    for child in list(text_el):
        text_el.remove(child)
    text_el.text = value


def _row_value(rows: dict[str, Any], key: str) -> str:
    val = str(rows.get(key) or "").strip()
    if not val:
        return "—"
    if len(val) > _ROW_TEXT_MAX_CHARS:
        return val[: _ROW_TEXT_MAX_CHARS - 1].rstrip() + "…"
    return val


def _confidence_label(conf: dict[str, Any], key: str) -> str:
    raw = conf.get(key)
    if raw is None:
        return "—"
    try:
        pct = max(0.0, min(1.0, float(raw))) * 100.0
    except (TypeError, ValueError):
        return "—"
    return f"{int(round(pct))}%"


def _status_fill(el: ET.Element | None, ok: bool) -> None:
    if el is None:
        return
    _set_paint(el, fill=_COLOR_STATUS_OK if ok else _COLOR_STATUS_BAD)


def apply_metadata_debug_svg_state(
    root: ET.Element,
    state: MainSettingsState,
    data: dict[str, Any],
) -> None:
    page_idx = max(0, min(len(METADATA_DEBUG_PAGES) - 1, int(state.metadata_debug_page)))
    page = METADATA_DEBUG_PAGES[page_idx]

    player_active = bool(data.get("player_active"))
    hdmi_active = bool(data.get("hdmi_active"))
    pigeon_active = player_active or hdmi_active

    # The three icon tiles are stacked at the same spot — show the current one.
    icon_ids = {
        "player": "metadata_player_icon",
        "hdmi": "metadata_hdmi_icon",
        "pigeon": "metadata_pigeon_icon",
    }
    for name, lid in icon_ids.items():
        _set_visible(_find_by_logical_id(root, lid), name == page)

    # Status LEDs inside the player / hdmi tiles.
    _status_fill(
        _find_by_logical_id(root, "settings_pigeon_07_player_status_icon"),
        player_active,
    )
    _status_fill(
        _find_by_logical_id(root, "settings_pigeon_08_hdmi_status_icon"),
        hdmi_active,
    )
    # Pigeon differentiates itself with a colored stroke instead of an LED.
    _set_visible(
        _find_by_logical_id(root, "settings_pigeon_07_metadata_status_icon"), False
    )
    pigeon_btn = _find_by_logical_id(root, "settings_pigeon_07_metadata_button")
    if pigeon_btn is not None:
        _set_paint(
            pigeon_btn,
            stroke=_COLOR_STATUS_OK if pigeon_active else _COLOR_STATUS_BAD,
        )
        pigeon_btn.set("stroke-width", "5")

    # Duplicate results block from the export — always drive ``metadata_results``.
    _set_visible(_find_by_logical_id(root, "metadata_results_player"), False)

    rows = data.get(page)
    rows = rows if isinstance(rows, dict) else {}
    field_texts = _group_texts(root, "metadata_fields")
    result_texts = _group_texts(root, "metadata_results")
    for i, key in enumerate(_ROW_KEYS):
        show_row = not (page == "player" and key == "year")
        if i < len(field_texts):
            _set_visible(field_texts[i], show_row)
        if i < len(result_texts):
            _set_visible(result_texts[i], show_row)
            if show_row:
                _set_flat_text(result_texts[i], _row_value(rows, key))

    # Per-row confidence column: pigeon page only.
    conf_group = _find_by_logical_id(root, "metadata_confidence")
    if page == "pigeon":
        _set_visible(conf_group, True)
        conf = rows.get("confidence")
        conf = conf if isinstance(conf, dict) else {}
        conf_texts = _group_texts(root, "metadata_confidence")
        for i, key in enumerate(_ROW_KEYS):
            if i < len(conf_texts):
                _set_flat_text(conf_texts[i], _confidence_label(conf, key))
    else:
        _set_visible(conf_group, False)

    # EXIT is the only actionable control on this screen — always highlighted.
    from pigeon.widgets.pigeon_settings import _sync_back_button, _sync_version_text

    _sync_back_button(root, selected=True)
    _sync_version_text(root, state)


def _full_theme_bgra(
    state: MainSettingsState,
    *,
    assets_dir: Path | str | None,
    path: Path,
) -> np.ndarray:
    from pigeon.widgets.main_settings import _draw_container_background_bgra

    ui_hex = str(getattr(state.theme, "ui", "#ff0013") or "#ff0013")
    adir = str(assets_dir if assets_dir is not None else path.parent.parent)
    key = (ui_hex, adir, int(DESIGN_W), int(DESIGN_H))
    cached = _THEME_BG_CACHE.get(key)
    if cached is not None:
        return cached
    bg_bgra = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
    bg_bgra[:, :, 3] = 255
    _draw_container_background_bgra(bg_bgra, ui_hex=ui_hex, assets_dir=adir)
    while len(_THEME_BG_CACHE) >= _THEME_BG_CACHE_MAX:
        _THEME_BG_CACHE.pop(next(iter(_THEME_BG_CACHE)))
    _THEME_BG_CACHE[key] = bg_bgra
    return bg_bgra


def render_metadata_debug_bgra(
    state: MainSettingsState | None = None,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
    data: dict[str, Any] | None = None,
) -> np.ndarray:
    path = (
        Path(svg_path)
        if svg_path is not None
        else default_metadata_debug_svg_path(assets_dir)
    )
    if not path.is_file():
        raise FileNotFoundError(f"metadata debug SVG not found: {path}")
    st = state if state is not None else MainSettingsState()
    root = _svg_tree_from_path(path)
    apply_metadata_debug_svg_state(root, st, data if data is not None else metadata_debug_data())
    _disable_embedded_settings_background_layers(root)
    _prune_display_none(root)
    from pigeon.widgets.settings_svg_text import rasterize_settings_svg_bgra

    ui_bgra = rasterize_settings_svg_bgra(
        root,
        width=DESIGN_W,
        height=DESIGN_H,
        font_mode="preferences",
    )
    bg = _full_theme_bgra(st, assets_dir=assets_dir, path=path)
    return _composite_bgra_over_bgra(bg, ui_bgra)


def clear_metadata_debug_render_caches() -> None:
    _SVG_TREE_TEMPLATES.clear()
    _THEME_BG_CACHE.clear()
