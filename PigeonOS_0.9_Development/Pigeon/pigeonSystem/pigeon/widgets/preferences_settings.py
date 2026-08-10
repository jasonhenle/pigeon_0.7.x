"""
Now-playing zone widget selector — ``settings_0.8/pigeon_settings_preferences.svg``.

Opened from pigeon device settings when **prefs** / settings is activated.

Navigation A (zones): pick zone 1–5 or BACK (BACK → pigeon settings).
Navigation B (widgets): with a zone locked selected, cycle widgets available
for that zone; activate confirms and returns to zones. BACK still exits to
pigeon settings.
"""

from __future__ import annotations

import copy
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import numpy as np

from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.widgets.main_settings import (
    MainSettingsState,
    _composite_bgra_over_bgra,
    _disable_embedded_settings_background_layers,
    _draw_container_background_bgra,
    _find_by_logical_id,
    _prune_display_none,
    _set_paint,
    _set_text_content,
    _set_visible,
)

SVG_NS = "http://www.w3.org/2000/svg"

# Crop the Illustrator board to the 800×480 preferences artboard.
_PREFS_VIEWBOX = (339.0, 440.0, 800.0, 480.0)

_COLOR_BLACK = "#000000"
_COLOR_WHITE = "#FFFFFF"
_COLOR_GRAY = "#808080"  # 50% gray for unavailable chrome

# Defaults match current now-playing layout.
DEFAULT_ZONE_WIDGETS: tuple[str, str, str, str, str] = (
    "clock",
    "poster",
    "volume",
    "cast_info",
    "now_playing",
)

# Spec catalog — which widgets may occupy each zone.
ZONE_WIDGET_CATALOG: dict[int, tuple[str, ...]] = {
    1: ("audio_levels", "clock", "poster", "volume", "now_playing", "cast_info"),
    2: ("audio_levels", "clock", "poster", "volume", "now_playing", "cast_info"),
    3: ("audio_levels", "clock", "poster", "volume", "now_playing", "cast_info"),
    4: ("cast_info",),
    5: ("now_playing", "cast_info"),
}

# Selector chrome groups (navigation B), left → right in the SVG.
_WIDGET_SELECTOR_ORDER: tuple[str, ...] = (
    "audio_levels",
    "clock",
    "poster",
    "volume",
    "now_playing",
    "cast_info",
)

# Match settings_main EXIT geometry on the preferences artboard (viewBox +339,+440).
_BACK_BUTTON_D = (
    "M426.443,535H376.367c-5.278,0-9.557-4.279-9.557-9.557V507.628"
    "c0-5.831,4.727-10.557,10.557-10.557h49.075c5.278,0,9.557,4.279,9.557,9.557"
    "v18.814C436,530.721,431.721,535,426.443,535z"
)
_BACK_TEXT_X = 401.405  # button center (design 62.405 + 339)
_BACK_TEXT_Y = 525.7403  # same baseline as settings_main EXIT
_COLOR_EXIT_FILL = "#202020"

_ZONE_NAV_ORDER: tuple[str, ...] = (
    "zone1",
    "zone2",
    "zone3",
    "zone4",
    "zone5",
    "exit",
)

_SVG_TREE_TEMPLATES: dict[tuple[str, int, int], ET.Element] = {}
_SVG_TREE_TEMPLATE_MAX = 4


def default_preferences_svg_path(assets_dir: Path | str | None = None) -> Path:
    env = os.environ.get("PIGEON_PREFERENCES_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "settings_0.8" / "pigeon_settings_preferences.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "settings_0.8" / "pigeon_settings_preferences.svg"


def _layer_key(el: ET.Element) -> str:
    dn = (el.get("data-name") or "").strip()
    raw = dn if dn else (el.get("id") or "").strip()
    if not dn and raw:
        raw = re.sub(r"-\d+$", "", raw)
    return re.sub(r"\s+", "_", raw)


def _normalize_zone_widgets(
    values: tuple[str, ...] | list[str] | dict[int, str] | None,
) -> tuple[str, str, str, str, str]:
    base = list(DEFAULT_ZONE_WIDGETS)
    if isinstance(values, dict):
        for z, name in values.items():
            try:
                zi = int(z)
            except (TypeError, ValueError):
                continue
            if 1 <= zi <= 5 and str(name or "").strip():
                base[zi - 1] = str(name).strip()
    elif isinstance(values, (tuple, list)):
        for i, name in enumerate(list(values)[:5]):
            if str(name or "").strip():
                base[i] = str(name).strip()
    # Clamp to catalog.
    out: list[str] = []
    for i, name in enumerate(base):
        zone = i + 1
        catalog = ZONE_WIDGET_CATALOG.get(zone, ())
        out.append(name if name in catalog else DEFAULT_ZONE_WIDGETS[i])
    return (out[0], out[1], out[2], out[3], out[4])


def read_now_playing_zone_widgets() -> tuple[str, str, str, str, str]:
    try:
        from pigeon.app_state import read_app_state

        raw = read_app_state().get("now_playing_zone_widgets")
    except Exception:
        raw = None
    if isinstance(raw, dict):
        return _normalize_zone_widgets(raw)
    if isinstance(raw, list):
        return _normalize_zone_widgets(raw)
    return DEFAULT_ZONE_WIDGETS


def write_now_playing_zone_widgets(
    values: tuple[str, ...] | list[str] | dict[int, str],
) -> tuple[str, str, str, str, str]:
    norm = _normalize_zone_widgets(values)
    try:
        from pigeon.app_state import write_app_state

        write_app_state(
            now_playing_zone_widgets={
                "1": norm[0],
                "2": norm[1],
                "3": norm[2],
                "4": norm[3],
                "5": norm[4],
            }
        )
    except Exception:
        pass
    return norm


def zone0_date_align(
    assignments: tuple[str, ...] | list[str] | dict[int, str] | None = None,
) -> str | None:
    """Where the zone0 date header sits on now-playing.

    - default → ``left``
    - zone1 poster → ``center``
    - zone1 + zone2 poster → ``right``
    - zone1 + zone2 + zone3 poster → ``None`` (header off)

    Preferences keeps this logic but does not display the header.
    """
    a = _normalize_zone_widgets(assignments)
    p1 = a[0] == "poster"
    p2 = a[1] == "poster"
    p3 = a[2] == "poster"
    if p1 and p2 and p3:
        return None
    if p1 and p2:
        return "right"
    if p1:
        return "center"
    return "left"


def preferences_zone_focus_ring() -> tuple[str, ...]:
    return _ZONE_NAV_ORDER


def preferences_widget_focus_ring(zone: int) -> tuple[str, ...]:
    """BACK + widgets available for ``zone`` (selector order)."""
    z = int(zone)
    available = set(ZONE_WIDGET_CATALOG.get(z, ()))
    ring: list[str] = ["exit"]
    for wid in _WIDGET_SELECTOR_ORDER:
        if wid in available:
            ring.append(wid)
    return tuple(ring)


def _svg_tree_from_path(path: Path) -> ET.Element:
    try:
        st = path.stat()
        key = (str(path.resolve()), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        key = (str(path), 0, 0)
    template = _SVG_TREE_TEMPLATES.get(key)
    if template is None:
        root = ET.parse(path).getroot()
        x, y, w, h = _PREFS_VIEWBOX
        root.set("viewBox", f"{x} {y} {w} {h}")
        root.set("width", str(DESIGN_W))
        root.set("height", str(DESIGN_H))
        if len(_SVG_TREE_TEMPLATES) >= _SVG_TREE_TEMPLATE_MAX:
            _SVG_TREE_TEMPLATES.clear()
        _SVG_TREE_TEMPLATES[key] = root
        template = root
    return copy.deepcopy(template)


def _paint_subtree(
    el: ET.Element | None,
    *,
    fill: str | None = None,
    stroke: str | None = None,
) -> None:
    if el is None:
        return
    tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
    if tag in ("path", "polygon", "polyline", "circle", "ellipse", "rect"):
        kwargs: dict[str, str] = {}
        if fill is not None:
            kwargs["fill"] = fill
        if stroke is not None:
            kwargs["stroke"] = stroke
        if kwargs:
            _set_paint(el, **kwargs)
    for child in list(el):
        _paint_subtree(child, fill=fill, stroke=stroke)


def _paint_text(el: ET.Element | None, color: str) -> None:
    if el is None:
        return
    nodes = [el] if el.tag.endswith("text") else [
        n for n in el.iter() if n.tag.endswith("text") or n.tag.endswith("tspan")
    ]
    if not nodes:
        nodes = [el]
    for node in nodes:
        _set_paint(node, fill=color)


def _zone_group_parts(group: ET.Element | None) -> tuple[ET.Element | None, ET.Element | None, ET.Element | None]:
    """Return (button, accent, text) for a selector_zoneN_group (ids may be missing)."""
    if group is None:
        return None, None, None
    kids = list(group)
    button = accent = text = None
    for child in kids:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        key = _layer_key(child)
        if tag == "text" or "text" in key:
            text = child
        elif tag == "circle":
            fill = (child.get("fill") or "").strip().lower()
            if fill in ("none",) or "accent" in key:
                accent = child if accent is None else accent
            else:
                # Missing fill or solid → button.
                if button is None:
                    button = child
                else:
                    accent = child
    # Fallback by order: button, accent, text.
    if button is None and kids:
        button = kids[0]
    if accent is None and len(kids) > 1:
        accent = kids[1]
    if text is None and len(kids) > 2:
        text = kids[2]
    # Ensure Illustrator-unnamed zone 2–5 kids get stable data-names.
    zkey = _layer_key(group)
    m = re.fullmatch(r"selector_zone(\d)_group", zkey)
    if m:
        z = m.group(1)
        if button is not None and not (button.get("id") or button.get("data-name")):
            button.set("data-name", f"selector_zone{z}_button")
        if accent is not None and not (accent.get("id") or accent.get("data-name")):
            accent.set("data-name", f"selector_zone{z}_accent")
        if text is not None and not (text.get("id") or text.get("data-name")):
            text.set("data-name", f"selector_zone{z}_text")
    return button, accent, text


def _widget_group_parts(
    group: ET.Element | None,
) -> tuple[ET.Element | None, ET.Element | None, ET.Element | None]:
    """Return (button path, accent path, text) inside a selector_*_group."""
    if group is None:
        return None, None, None
    button = accent = text = None
    # audio_levels nests one extra group.
    scope = group
    inner = None
    for child in list(group):
        if _layer_key(child) == "selector_audio_level_group":
            inner = child
            break
    if inner is not None:
        scope = inner
    for child in list(scope):
        key = _layer_key(child)
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "text" or "text" in key:
            text = child
        elif "accent" in key:
            accent = child
        elif "button" in key or tag == "path":
            if button is None:
                button = child
            elif accent is None:
                accent = child
    return button, accent, text


def _apply_zone_chrome(group: ET.Element | None, *, selected: bool) -> None:
    button, accent, text = _zone_group_parts(group)
    if selected:
        _paint_subtree(button, fill=_COLOR_WHITE, stroke=_COLOR_BLACK)
        _paint_text(text, _COLOR_BLACK)
    else:
        _paint_subtree(button, fill=_COLOR_BLACK, stroke=_COLOR_BLACK)
        _paint_text(text, _COLOR_WHITE)
    # Accent stays white stroke / no fill in both states.
    _paint_subtree(accent, fill="none", stroke=_COLOR_WHITE)


def _apply_widget_chrome(
    group: ET.Element | None,
    *,
    selected: bool,
    available: bool,
    idle_fill: str | None = None,
) -> None:
    button, accent, text = _widget_group_parts(group)
    rest_fill = idle_fill if idle_fill is not None else _COLOR_BLACK
    if not available:
        _paint_subtree(button, fill=rest_fill, stroke=_COLOR_BLACK)
        _paint_subtree(accent, fill="none", stroke=_COLOR_GRAY)
        _paint_text(text, _COLOR_GRAY)
        return
    if selected:
        _paint_subtree(button, fill=_COLOR_WHITE, stroke=_COLOR_BLACK)
        _paint_text(text, _COLOR_BLACK)
        _paint_subtree(accent, fill="none", stroke=_COLOR_WHITE)
        return
    _paint_subtree(button, fill=rest_fill, stroke=_COLOR_BLACK)
    _paint_text(text, _COLOR_WHITE)
    _paint_subtree(accent, fill="none", stroke=_COLOR_WHITE)


def _sync_preferences_back_button(root: ET.Element) -> None:
    """Match settings_main EXIT placement; label centered as BACK."""
    group = _find_by_logical_id(root, "selector_exit_group")
    button, accent, text = _widget_group_parts(group)
    for path_el in (button, accent):
        if path_el is not None:
            path_el.set("d", _BACK_BUTTON_D)
            if path_el is button and not (path_el.get("fill") or "").strip():
                path_el.set("fill", _COLOR_EXIT_FILL)
    if text is not None:
        text.set("transform", f"translate({_BACK_TEXT_X:.4f} {_BACK_TEXT_Y:.4f})")
        text.set("text-anchor", "middle")
        text.set("font-family", "Digital-7, Digital-7")
        text.set("font-size", "29")
        _set_text_content(text, "BACK")


def _selector_pill_center(path_el: ET.Element | None) -> tuple[float, float] | None:
    """Center of the shared rounded selector pill path (Illustrator exit-button shape)."""
    if path_el is None:
        return None
    d = path_el.get("d") or ""
    m = re.match(
        r"[Mm]\s*([-\d.]+)\s*,\s*([-\d.]+)\s*[Hh]\s*([-\d.]+)",
        d.strip(),
    )
    if not m:
        return None
    mx = float(m.group(1))
    my = float(m.group(2))
    h = float(m.group(3))
    # Same geometry as other selector pills: right radius 9.56, left span 50.08 / 9.56.
    right = mx + h + 9.56
    left = right - 50.08 - 9.56
    top = my
    bottom = my + 9.56 + 18.81 + 9.56
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _selector_label_font(size_px: int = 15):
    from PIL import ImageFont

    from pigeon.font_paths import resolve_ui_font_extrabold

    path = resolve_ui_font_extrabold()
    if path:
        try:
            return ImageFont.truetype(str(path), int(size_px))
        except OSError:
            pass
    return ImageFont.load_default()


def _ink_bbox_ms(font, text: str, *, y: float = 0.0) -> tuple[float, float]:
    """Top/bottom ink for Pillow ``ms`` (middle/baseline) at ``y``."""
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    _l, top, _r, bottom = draw.textbbox((0, y), text, font=font, anchor="ms")
    return float(top), float(bottom)


def _set_one_line_centered_label(
    text_el: ET.Element | None,
    *,
    cx: float,
    cy: float,
    label: str,
) -> None:
    """Center a single-line Sharp label on ``(cx, cy)`` inside a selector pill."""
    if text_el is None:
        return
    font = _selector_label_font(15)
    top, bottom = _ink_bbox_ms(font, label, y=0.0)
    baseline = float(cy) - (top + bottom) / 2.0
    text_el.set("transform", f"translate({cx:.4f} {baseline:.4f})")
    text_el.set("text-anchor", "middle")
    if "dominant-baseline" in text_el.attrib:
        del text_el.attrib["dominant-baseline"]
    for child in list(text_el):
        text_el.remove(child)
    text_el.text = None
    tspan = ET.SubElement(text_el, f"{{{SVG_NS}}}tspan")
    tspan.set("x", "0")
    tspan.set("y", "0")
    tspan.text = label


def _set_two_line_centered_label(
    text_el: ET.Element | None,
    *,
    cx: float,
    cy: float,
    line1: str,
    line2: str,
    line_gap: float = 14.0,
) -> None:
    """Center a two-line Sharp label block on ``(cx, cy)`` inside a selector pill."""
    if text_el is None:
        return
    font = _selector_label_font(15)
    gap = float(line_gap)
    t1, b1 = _ink_bbox_ms(font, line1, y=0.0)
    t2, b2 = _ink_bbox_ms(font, line2, y=gap)
    mid0 = (min(t1, t2) + max(b1, b2)) / 2.0
    first_baseline = float(cy) - mid0
    text_el.set("transform", f"translate({cx:.4f} {first_baseline:.4f})")
    text_el.set("text-anchor", "middle")
    if "dominant-baseline" in text_el.attrib:
        del text_el.attrib["dominant-baseline"]
    for child in list(text_el):
        text_el.remove(child)
    text_el.text = None
    span1 = ET.SubElement(text_el, f"{{{SVG_NS}}}tspan")
    span1.set("x", "0")
    span1.set("y", "0")
    span1.text = line1
    span2 = ET.SubElement(text_el, f"{{{SVG_NS}}}tspan")
    span2.set("x", "0")
    span2.set("y", f"{gap:g}")
    span2.text = line2


_SELECTOR_LABELS: dict[str, tuple[str, ...]] = {
    "audio_levels": ("audio", "levels"),
    "clock": ("clock",),
    "poster": ("poster", "art"),
    "volume": ("volume",),
    "now_playing": ("now", "playing"),
    "cast_info": ("cast", "info"),
}


def _sync_preferences_selector_labels(root: ET.Element) -> None:
    """Center every widget-selector label inside its pill (H + V)."""
    for wid, lines in _SELECTOR_LABELS.items():
        group = _selector_group_for_widget(root, wid)
        button, _accent, text = _widget_group_parts(group)
        center = _selector_pill_center(button)
        if center is None or text is None:
            continue
        if len(lines) >= 2:
            _set_two_line_centered_label(
                text,
                cx=center[0],
                cy=center[1],
                line1=lines[0],
                line2=lines[1],
            )
        else:
            _set_one_line_centered_label(
                text,
                cx=center[0],
                cy=center[1],
                label=lines[0],
            )


def _sync_preferences_zone_numbers(root: ET.Element) -> None:
    """Center zone numerals in their selector circles."""
    for zone in (1, 2, 3, 4, 5):
        group = _find_by_logical_id(root, f"selector_zone{zone}_group")
        button, _accent, text = _zone_group_parts(group)
        if button is None or text is None:
            continue
        try:
            cx = float(button.get("cx") or 0.0)
            cy = float(button.get("cy") or 0.0)
        except ValueError:
            continue
        text.set("transform", f"translate({cx:.4f} {cy:.4f})")
        text.set("text-anchor", "middle")
        text.set("dominant-baseline", "middle")
        # Keep a single centered digit tspan.
        digit = str(zone)
        for child in list(text):
            text.remove(child)
        text.text = None
        tspan = ET.SubElement(text, f"{{{SVG_NS}}}tspan")
        tspan.set("x", "0")
        tspan.set("y", "0")
        tspan.text = digit


def _prefs_volume_group(root: ET.Element, zone: int) -> ET.Element | None:
    from pigeon.widgets.view_circles import _find_by_key

    return _find_by_key(root, f"zone{zone}_volume_group")


def _prefs_find_in_group(
    group: ET.Element | None,
    *,
    key_endswith: str,
) -> ET.Element | None:
    """Find a descendant by layer-key suffix (Illustrator often reuses zone1 ids)."""
    if group is None:
        return None
    from pigeon.widgets.view_circles import _layer_key

    want = key_endswith
    for el in group.iter():
        key = _layer_key(el)
        if key == want or key.endswith(want):
            return el
    return None


def _prefs_volume_center(root: ET.Element, zone: int) -> tuple[float, float] | None:
    group = _prefs_volume_group(root, zone)
    for suffix in (
        "volume_container",
        "volume_selected_button",
        "volume_deselected_buton",
    ):
        el = _prefs_find_in_group(group, key_endswith=suffix)
        if el is None:
            continue
        try:
            return float(el.get("cx") or 0.0), float(el.get("cy") or 0.0)
        except ValueError:
            continue
    return None


def _center_digital_text(
    text_el: ET.Element | None,
    *,
    cx: float,
    cy: float,
    label: str,
) -> None:
    if text_el is None:
        return
    # Prefer the nested <text> if this is a group wrapper.
    target = text_el
    if not text_el.tag.endswith("text"):
        nested = next((n for n in text_el.iter() if n.tag.endswith("text")), None)
        if nested is not None:
            target = nested
    target.set("transform", f"translate({cx:.4f} {cy:.4f})")
    target.set("text-anchor", "middle")
    target.set("dominant-baseline", "middle")
    _set_text_content(target, label)


def _apply_preferences_zone_dynamics(
    root: ET.Element,
    assignments: tuple[str, str, str, str, str],
    *,
    now: datetime | None = None,
) -> None:
    """Drive clock ticks / digital time; adapt volume vs circular now-playing.

    When nothing is playing the SVG keeps its static examples (volume level,
    cast, poster, zone5 bar). Clock always follows wall time.
    """
    from pigeon.widgets.view_circles import (
        _apply_clock_accent_fills,
        _apply_clock_ticks,
        _clock_hhmm,
        _find_by_key,
    )

    dt = now if now is not None else datetime.now()
    hhmm = _clock_hhmm(dt)
    _apply_clock_accent_fills(root)

    for zone in (1, 2, 3):
        widget = assignments[zone - 1]
        if widget == "clock":
            _apply_clock_ticks(root, zone, dt)
            clock = _find_by_key(root, f"zone{zone}_clock_group")
            digital = _find_by_key(root, f"zone{zone}_clock_digital_text")
            if clock is not None:
                exterior = _find_by_key(clock, f"zone{zone}_clock_exterior_accent")
                try:
                    cx = float((exterior.get("cx") if exterior is not None else None) or 0.0)
                    cy = float((exterior.get("cy") if exterior is not None else None) or 0.0)
                except ValueError:
                    cx = cy = 0.0
                if cx and cy:
                    _center_digital_text(digital, cx=cx, cy=cy, label=hhmm)
            continue

        if widget not in ("volume", "now_playing"):
            continue

        vol_group = _prefs_volume_group(root, zone)
        center = _prefs_volume_center(root, zone)
        vol_text = _prefs_find_in_group(vol_group, key_endswith="volume_text")
        cfg_text = _prefs_find_in_group(
            vol_group, key_endswith="voume_audio_config_text"
        ) or _prefs_find_in_group(vol_group, key_endswith="audio_config_text")
        if widget == "now_playing":
            # Circular now-playing: no audio config; readout is time of day.
            if cfg_text is not None:
                _set_visible(cfg_text, False)
            if center is not None:
                _center_digital_text(vol_text, cx=center[0], cy=center[1], label=hhmm)
            else:
                _set_text_content(vol_text, hhmm)
            # Keep the static red/grey pie as the idle progress example.
            continue

        # Volume widget — keep static example copy; center it in the ring.
        if center is not None:
            vol_label = (
                "".join(vol_text.itertext()).strip() if vol_text is not None else "-22.5"
            )
            if not vol_label:
                vol_label = "-22.5"
            _center_digital_text(
                vol_text, cx=center[0], cy=center[1] - 10.0, label=vol_label
            )
            if cfg_text is not None:
                cfg_label = "".join(cfg_text.itertext()).strip() or "multi-in > auro3d"
                _center_digital_text(
                    cfg_text,
                    cx=center[0],
                    cy=center[1] + 16.0,
                    label=cfg_label,
                )


def _widget_preview_keys(zone: int, widget: str) -> tuple[str, ...]:
    """Layer keys under the preferences SVG that belong to ``widget`` in ``zone``."""
    z = int(zone)
    w = str(widget)
    if z == 4:
        return ("zone4_cast_info_group",) if w == "cast_info" else ()
    if z == 5:
        # Cast sits beside the bar under a shared outer ``zone5_now_playing_group``.
        # Toggle the inner bar / cast kids — not the outer container.
        if w == "now_playing":
            return ("zone5_now_playing_bar", "zone5_now_playing_group")
        if w == "cast_info":
            return ("zone5_cast_info_group", "zone5_cast_group")
        return ()
    if w == "audio_levels":
        return (f"zone{z}_audio_levels_group",)
    if w == "clock":
        return (f"zone{z}_clock_group",)
    if w == "volume":
        return (f"zone{z}_volume_group",)
    if w == "now_playing":
        # Zones 1–3 reuse the volume ring chrome for playback progress.
        return (f"zone{z}_volume_group",)
    if w == "cast_info":
        return (f"zone{z}_cast_info_group", f"zone{z}_cast_group")
    if w == "poster":
        return (
            f"zone{z}_poster_group",
            f"zone{z}_poster_art_group",
            f"zone{z}_poster_2x3",
            f"zone{z}_2x3_poster_group",
            f"zone{z}_album_art_1x1",
        )
    return ()


def _zone5_container_and_kids(
    root: ET.Element,
) -> tuple[ET.Element | None, ET.Element | None, ET.Element | None]:
    """Return ``(outer, bar_group, cast_group)`` for zone5 preview layers.

    Current Illustrator export nests cast under the outer now-playing group::

        zone5_now_playing_group
          zone5_cast_info_group
          zone5_now_playing_group   (inner bar / CTI)
    """
    outer = _find_by_logical_id(root, "zone5_now_playing_group")
    if outer is None:
        return None, None, None
    bar = cast = None
    for child in list(outer):
        key = _layer_key(child)
        if key in ("zone5_cast_info_group", "zone5_cast_group"):
            cast = child
        elif "now_playing" in key:
            bar = child
    # Flat export fallback (siblings, not nested).
    if cast is None:
        cast = _find_by_logical_id(root, "zone5_cast_info_group") or _find_by_logical_id(
            root, "zone5_cast_group"
        )
    return outer, bar, cast


def _iter_zone_widget_layers(root: ET.Element, zone: int):
    """Yield direct widget layers for a zone (preferences preview)."""
    z = int(zone)
    if z in (1, 2, 3):
        parent = _find_by_logical_id(root, f"zone{z}_group")
        if parent is None:
            return
        for child in list(parent):
            yield child
        return
    if z == 4:
        el = _find_by_logical_id(root, "zone4_cast_info_group")
        if el is not None:
            yield el
        return
    if z == 5:
        _outer, bar, cast = _zone5_container_and_kids(root)
        if bar is not None:
            yield bar
        if cast is not None:
            yield cast


def _apply_zone0_date_preview(
    root: ET.Element,
    _assignments: tuple[str, str, str, str, str],
) -> None:
    """Hide zone0 date chrome on preferences (align logic still drives now-playing)."""
    header = _find_by_logical_id(root, "zone0_header_group")
    _set_visible(header, False)
    for name in ("left", "center", "right"):
        _set_visible(_find_by_logical_id(root, f"zone0_date_{name}_text"), False)


def _apply_zone_preview(root: ET.Element, zone: int, widget: str) -> None:
    z = int(zone)
    w = str(widget)
    if z == 5:
        outer, bar, cast = _zone5_container_and_kids(root)
        # Keep the outer wrapper visible so nested cast/bar can show.
        if outer is not None:
            _set_visible(outer, True)
        if bar is not None:
            # Inner bar group often reuses the now_playing name — show only for bar widget.
            _set_visible(bar, w == "now_playing")
        if cast is not None:
            _set_visible(cast, w == "cast_info")
        return

    want = set(_widget_preview_keys(z, w))
    for el in _iter_zone_widget_layers(root, z):
        key = _layer_key(el)
        show = key in want or any(key.startswith(prefix) for prefix in want)
        # Nested poster groups: show parent if any child key matches.
        if not show and "poster" in key and any("poster" in prefix for prefix in want):
            show = True
        _set_visible(el, show)


def _selector_group_for_widget(root: ET.Element, widget: str) -> ET.Element | None:
    names = {
        "audio_levels": "selector_audio_levels_group",
        "clock": "selector_clock_group",
        "poster": "selector_poster_art_group",
        "volume": "selector_volume_group",
        "now_playing": "selector_now_playing_group",
        "cast_info": "selector_cast_info_group",
        "exit": "selector_exit_group",
    }
    name = names.get(widget)
    if not name:
        return None
    return _find_by_logical_id(root, name)


def apply_preferences_svg_state(root: ET.Element, state: MainSettingsState) -> None:
    nav = str(getattr(state, "preferences_nav", "zones") or "zones")
    active_zone = int(getattr(state, "preferences_active_zone", 0) or 0)
    assignments = _normalize_zone_widgets(
        getattr(state, "preferences_zone_widgets", None) or DEFAULT_ZONE_WIDGETS
    )
    focused = str(getattr(state, "preferences_focused_id", "") or "")

    # Zone preview layers follow current assignments (live while browsing widgets).
    for zone in (1, 2, 3, 4, 5):
        _apply_zone_preview(root, zone, assignments[zone - 1])

    # Zone0 date: left / center / right / off from poster occupancy.
    _apply_zone0_date_preview(root, assignments)

    # Zone selector chrome.
    for zone in (1, 2, 3, 4, 5):
        group = _find_by_logical_id(root, f"selector_zone{zone}_group")
        if nav == "widgets" and active_zone == zone:
            selected = True  # locked selected while editing widgets
        elif nav == "zones":
            selected = focused == f"zone{zone}"
        else:
            selected = False
        _apply_zone_chrome(group, selected=selected)

    # Live clock ticks / digital time; volume keeps static example when idle.
    _apply_preferences_zone_dynamics(root, assignments)

    # Widget selector chrome + BACK (settings_main EXIT twin).
    _sync_preferences_back_button(root)
    _sync_preferences_selector_labels(root)
    _sync_preferences_zone_numbers(root)
    available: set[str] = set()
    if nav == "widgets" and active_zone in ZONE_WIDGET_CATALOG:
        available = set(ZONE_WIDGET_CATALOG[active_zone])

    for wid in _WIDGET_SELECTOR_ORDER:
        group = _selector_group_for_widget(root, wid)
        if nav == "zones":
            # Resting available look while choosing a zone.
            _apply_widget_chrome(group, selected=False, available=True)
        else:
            _apply_widget_chrome(
                group,
                selected=(focused == wid),
                available=(wid in available),
            )

    exit_group = _selector_group_for_widget(root, "exit")
    exit_selected = focused == "exit"
    _apply_widget_chrome(
        exit_group,
        selected=exit_selected,
        available=True,
        idle_fill=_COLOR_EXIT_FILL,
    )


def render_preferences_settings_bgra(
    state: MainSettingsState | None = None,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    if svg_path is not None:
        path = Path(svg_path)
    else:
        path = default_preferences_svg_path(assets_dir)
    if not path.is_file():
        raise FileNotFoundError(f"preferences SVG not found: {path}")

    st = state if state is not None else MainSettingsState()
    root = _svg_tree_from_path(path)
    apply_preferences_svg_state(root, st)
    _disable_embedded_settings_background_layers(root)
    _prune_display_none(root)
    # BACK → Digital-7; other labels keep their SVG font families.
    from pigeon.widgets.settings_svg_text import rasterize_settings_svg_bgra

    ui_bgra = rasterize_settings_svg_bgra(
        root,
        width=DESIGN_W,
        height=DESIGN_H,
        font_mode="preferences",
    )
    bg_bgra = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
    bg_bgra[:, :, :3] = 0
    bg_bgra[:, :, 3] = 255
    _draw_container_background_bgra(
        bg_bgra,
        ui_hex=st.theme.ui,
        assets_dir=assets_dir if assets_dir is not None else path.parent.parent,
    )
    return _composite_bgra_over_bgra(bg_bgra, ui_bgra)


__all__ = [
    "DEFAULT_ZONE_WIDGETS",
    "ZONE_WIDGET_CATALOG",
    "apply_preferences_svg_state",
    "default_preferences_svg_path",
    "preferences_widget_focus_ring",
    "preferences_zone_focus_ring",
    "read_now_playing_zone_widgets",
    "render_preferences_settings_bgra",
    "write_now_playing_zone_widgets",
    "zone0_date_align",
]
