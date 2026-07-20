"""
Pigeon 0.8 main settings — SVG chrome from ``settings_0.8/settings_main.svg``.

Renders the primary settings menu (800×400 BGRA) with left/right focus navigation
and selection recoloring. Full keyboard / network-picker systems are stubbed with
path constants for later wiring.
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

import cv2
import numpy as np

from pigeon.compositing import alpha_blend_bgra_over_bgr

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# --- Colors (settingInstructions_0.8.0) ---
COLOR_SELECTED = "#02e900"
COLOR_DESELECTED = "#202020"
COLOR_UI_DEFAULT = "#ff0013"
COLOR_ACCENT_DEFAULT = "#FFFFFF"
COLOR_INACTIVE = "#404040"
COLOR_VERSION_TEXT = "#000000"

DESIGN_W = 800
DESIGN_H = 400

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
    "main_dual_location_button",
    "main_dual_network_button",
    "main_box1_button",
    "main_box2_button",
    "main_box3_button",
    "main_exit_button",
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
        "main_dual_network_wifi1_icon",
        "main_dual_network_wifi2_icon",
        "main_dual_network_wifi3_icon",
        "main_dual_network_wifi_fail_icon",
    ),
    "main_box1_button": (
        "main_box1_text",
        "main_box1_loation1_text",
        "main_device_name_text",
        "main_device_ip_text",
        "main_box1_left_icon",
        "main_box1_right_icon",
        "main_box1_up_icon",
        "main_box1_down_icon",
        "main_box1_circle1_icon",
        "main_box1_circle2_icon",
        "main_box1_circle3_icon",
        "main_box1_search_icon",
    ),
    "main_box2_button": (
        "main_box2_loation1_text",
        "main_device_name_text",
        "main_device_ip_text",
        "main_box2_up_icon",
        "main_box2_down_icon",
        "main_box2_circle1_icon",
        "main_box2_circle2_icon",
        "main_box2_circle3_icon",
        "main_box2_search_icon",
        "main_box2_+_icon",
    ),
    "main_box3_button": (
        "main_box3_loation1_text",
        "main_box3_device_name_text",
        "main_box3_device_ip_text",
        "main_box3_left_icon",
        "main_box3_right_icon",
        "main_box3_up_icon",
        "main_box3_down_icon",
        "main_box3_circle1_icon",
        "main_box3_circle2_icon",
        "main_box3_circle3_icon",
        "main_box3_search_icon",
    ),
    "main_exit_button": (
        "main_exit_text",
        "main_exit_icon",
    ),
    "main_network_picker_button": (
        "main_network_picker_row1_text",
        "main_network_picker_row2_text",
        "main_network_picker_row3_text",
        "main_network_picker_up_icon",
        "main_network_picker_down_icon",
    ),
}

_HIDE_ALWAYS_LOGICAL: tuple[str, ...] = (
    "keyboardtemp",
)

_BUTTON_FILL_CANDIDATES = frozenset(
    {
        COLOR_SELECTED.lower(),
        COLOR_DESELECTED.lower(),
        "#ffffff",
        "#000013",
        "#000000",
        "white",
        "black",
    }
)
_CONTRAST_SWAP_CANDIDATES = frozenset(
    {
        COLOR_SELECTED.lower(),
        COLOR_DESELECTED.lower(),
        "#ffffff",
        "#000013",
        "#000000",
        COLOR_UI_DEFAULT.lower(),
        "white",
        "black",
    }
)


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
    show_box1: bool = True
    show_box2: bool = True
    show_box3: bool = True
    # Focus ring rebuilt when panel visibility changes.
    focus_ring: tuple[str, ...] = field(default_factory=tuple)

    def ensure_focus_ring(self) -> None:
        ring = list(_PRIMARY_FOCUS_CANDIDATES)
        if not self.show_box1:
            ring = [x for x in ring if x != "main_box1_button"]
        if not self.show_box2:
            ring = [x for x in ring if x != "main_box2_button"]
        if not self.show_box3:
            ring = [x for x in ring if x != "main_box3_button"]
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
        self.ensure_focus_ring()
        n = len(self.focus_ring)
        step = 1 if forward else -1
        self.focus_index = (int(self.focus_index) + step) % n


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
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        if raw == encoded or raw == logical_id:
            return el
        if _normalize_logical(raw) == want:
            return el
        # Encoded id with trailing AI uniqueness still attached.
        if raw.startswith(encoded + "_") and _AI_SUFFIX_RE.search("_" + raw[len(encoded) + 1 :]):
            return el
        decoded = decode_svg_id(raw)
        if decoded.startswith(want + "_") and _AI_SUFFIX_RE.search(decoded[len(want) :]):
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
    if el is None:
        return
    if visible:
        el.attrib.pop("display", None)
        style = el.get("style") or ""
        if "display:" in style:
            el.set("style", re.sub(r"display:\s*[^;]+;?", "", style).strip().rstrip(";"))
    else:
        el.set("display", "none")
        style = el.get("style") or ""
        if "display:" in style:
            el.set("style", re.sub(r"display:\s*[^;]+", "display:none", style))
        elif style:
            el.set("style", f"{style};display:none")
        else:
            el.set("style", "display:none")


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
    if c.startswith("url("):
        return None
    if not c.startswith("#") and _HEX_RE.match(f"#{c}"):
        c = f"#{c}"
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
    """Green text/icons on deselected; black on selected. Leaves accent alone."""
    if group is None:
        return
    contrast = theme.deselected if selected else theme.selected
    for node in group.iter():
        nid = _normalize_logical(node.get("id") or "")
        if nid.endswith("_accent") or "_accent_" in nid:
            continue
        fill, stroke = _iter_style_fill_stroke(node)
        if fill and fill not in ("none", "transparent") and fill in _CONTRAST_SWAP_CANDIDATES:
            _set_paint(node, fill=contrast)
        if stroke and stroke not in ("none", "transparent") and stroke in _CONTRAST_SWAP_CANDIDATES:
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
            if stroke in ("#ffffff", "white", COLOR_ACCENT_DEFAULT.lower(), "#ff0013"):
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

    for box_id, show in (
        ("main_box1", state.show_box1),
        ("main_box2", state.show_box2),
        ("main_box3", state.show_box3),
    ):
        _set_visible(_find_by_logical_id(root, box_id), show)

    # Apply theme accent globally to *_accent layers (selection does not change accent).
    for el in root.iter():
        raw = el.get("id") or ""
        if not raw:
            continue
        logical = _normalize_logical(raw)
        if logical.endswith("_accent") or re.search(r"_accent(_|$)", logical):
            _apply_accent_paint(el, theme.accent)

    # Dynamic text stubs.
    _set_text_content(_find_by_logical_id(root, "main_dual_location_text"), state.location_name)
    _set_text_content(_find_by_logical_id(root, "main_dual_network_name_text"), state.network_name)
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

    # WiFi level stub: show matching ring count / fail icon.
    level = max(0, min(3, int(state.wifi_level)))
    for i in (1, 2, 3):
        icon = _find_by_logical_id(root, f"main_dual_network_wifi{i}_icon")
        _set_visible(icon, level > 0 and i <= level)
    fail = _find_by_logical_id(root, "main_dual_network_wifi_fail_icon")
    _set_visible(fail, level == 0)

    focused = state.focused_id
    ring = discover_focus_ring_in_svg(root, state)
    if focused not in ring and ring:
        # Snap to a ring entry that exists.
        state.focus_ring = ring
        state.focus_index = 0
        focused = state.focused_id

    for logical in ring:
        selected = logical == focused
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

        # Exit text lives under main_exit_icon; ensure EXIT contrast.
        if logical == "main_exit_button":
            for assoc_el in _find_all_by_logical_id(root, "main_exit_text"):
                _apply_contrast_paint(assoc_el, selected=selected, theme=theme)


def _svg_tree_from_path(path: Path) -> ET.Element:
    tree = ET.parse(path)
    root = tree.getroot()
    # Crop the 800×480 artboard to the 800×400 design band (matches settings_page).
    root.set("viewBox", "0 40 800 400")
    root.set("width", str(DESIGN_W))
    root.set("height", str(DESIGN_H))
    return root


def _rasterize_svg_tree(root: ET.Element) -> np.ndarray:
    """Return BGRA uint8 (DESIGN_H × DESIGN_W). Uses PyMuPDF; cairosvg if available."""
    svg_bytes = ET.tostring(root, encoding="utf-8")
    last_err: Exception | None = None

    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=svg_bytes, filetype="svg")
        page = doc[0]
        pix = page.get_pixmap(
            matrix=fitz.Matrix(DESIGN_W / page.rect.width, DESIGN_H / page.rect.height)
        )
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGBA2BGRA)
        else:
            bgra = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGRA)
        if bgra.shape[0] != DESIGN_H or bgra.shape[1] != DESIGN_W:
            bgra = cv2.resize(bgra, (DESIGN_W, DESIGN_H), interpolation=cv2.INTER_AREA)
        return bgra
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
            output_width=DESIGN_W,
            output_height=DESIGN_H,
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
        if bgra.shape[0] != DESIGN_H or bgra.shape[1] != DESIGN_W:
            bgra = cv2.resize(bgra, (DESIGN_W, DESIGN_H), interpolation=cv2.INTER_AREA)
        return bgra
    except ImportError as exc:
        last_err = exc
    except OSError as exc:
        last_err = exc
    except Exception as exc:
        last_err = exc

    msg = "Main settings needs PyMuPDF (pip install pymupdf) or cairosvg with system cairo."
    if last_err is not None:
        raise RuntimeError(msg) from last_err
    raise RuntimeError(msg)


def render_main_settings_bgra(
    state: MainSettingsState | None = None,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    """Load settings_main.svg, apply ``state``, return 800×400 BGRA."""
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
    return _rasterize_svg_tree(root)


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

    @property
    def state(self) -> MainSettingsState:
        return self._state

    def invalidate(self) -> None:
        self._cached_bgra = None
        self._cached_sig = None

    def _state_sig(self) -> tuple[object, ...]:
        st = self._state
        th = st.theme
        return (
            int(st.focus_index),
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
            bool(st.show_box1),
            bool(st.show_box2),
            bool(st.show_box3),
            str(self._svg_path or ""),
            str(self._assets_dir or ""),
        )

    def navigate(self, forward: bool = True) -> None:
        self._state.navigate(forward=forward)
        self.invalidate()

    def activate(self) -> str:
        """Return an action string for the focused control (stub handlers)."""
        focused = self._state.focused_id
        return _ACTIVATE_ACTIONS.get(focused, f"activate:{focused}")

    def bgra_frame(self) -> np.ndarray | None:
        sig = self._state_sig()
        if self._cached_bgra is not None and self._cached_sig == sig:
            return self._cached_bgra
        try:
            frame = render_main_settings_bgra(
                self._state,
                svg_path=self._svg_path,
                assets_dir=self._assets_dir,
            )
        except Exception:
            return self._cached_bgra
        self._cached_sig = sig
        self._cached_bgra = frame
        return frame

    def render(self, canvas_bgr: np.ndarray) -> None:
        """Paste main settings onto a design-sized BGR canvas (scaled to fill)."""
        frame = self.bgra_frame()
        if frame is None or canvas_bgr is None or canvas_bgr.size == 0:
            return
        ch, cw = int(canvas_bgr.shape[0]), int(canvas_bgr.shape[1])
        if frame.shape[0] != ch or frame.shape[1] != cw:
            frame = cv2.resize(frame, (cw, ch), interpolation=cv2.INTER_AREA)
        canvas_bgr[:] = alpha_blend_bgra_over_bgr(canvas_bgr, frame)

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
