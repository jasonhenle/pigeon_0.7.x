"""
Pigeon device settings — 12-tile grid (settings home).

Opened from main settings box1 (device panel). Shared settings theme background
under a 6×2 grid of filled, rounded tiles. Focused tile uses the settings UI
color. Existing destinations: GENERAL → preferences, COLORS → color page,
UPDATE → update popup.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.widgets.main_settings import MainSettingsState

# ---------------------------------------------------------------------------
# Focus / tile catalog (row-major: 6×2)
# ---------------------------------------------------------------------------

_PIGEON_FOCUS_RING: tuple[str, ...] = (
    "pigeon_back",
    "general_button",
    "colors_button",
    "display_button",
    "network_button",
    "audio_button",
    "time_button",
    "wifi_button",
    "music_button",
    "bluetooth_button",
    "system_button",
    "update_button",
    "about_button",
)

# Legacy aliases kept so older focus indices / saved expectations still resolve.
_FOCUS_ALIASES: dict[str, str] = {
    "prefs_button": "general_button",
    "color_button": "colors_button",
}


@dataclass(frozen=True)
class _Tile:
    focus_id: str
    label: str
    icon: str  # general | colors | display | update | about | none


_TILES: tuple[_Tile, ...] = (
    _Tile("general_button", "GENERAL", "general"),
    _Tile("colors_button", "COLORS", "colors"),
    _Tile("display_button", "DISPLAY", "display"),
    _Tile("network_button", "NETWORK", "none"),
    _Tile("audio_button", "AUDIO", "none"),
    _Tile("time_button", "TIME", "none"),
    _Tile("wifi_button", "WIFI", "none"),
    _Tile("music_button", "MUSIC", "none"),
    _Tile("bluetooth_button", "BLUETOOTH", "none"),
    _Tile("system_button", "SYSTEM", "none"),
    _Tile("update_button", "UPDATE", "update"),
    _Tile("about_button", "ABOUT", "about"),
)

# Layout (design 800×480).
_COLS = 6
_ROWS = 2
_TILE = 96
_GAP_X = 18
_GAP_Y = 28
_GRID_W = _COLS * _TILE + (_COLS - 1) * _GAP_X
_GRID_H = _ROWS * _TILE + (_ROWS - 1) * _GAP_Y
_GRID_X = (DESIGN_W - _GRID_W) // 2
_GRID_Y = 118
_LABEL_GAP = 10
_LABEL_SIZE = 13
_CORNER = 10
_BRACKET = 10
_STROKE = 2

_HEADER_SETTINGS_SIZE = 36
_HEADER_PIGEON_SIZE = 18
_HEADER_META_SIZE = 16
_HEADER_X = 36
_HEADER_SETTINGS_Y = 36
_HEADER_PIGEON_Y = 72
_HEADER_META_Y = 40


def default_pigeon_settings_svg_path(assets_dir: Path | str | None = None) -> Path:
    """Legacy path retained for callers / env overrides (grid is drawn in code)."""
    env = os.environ.get("PIGEON_PIGEON_SETTINGS_SVG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if assets_dir is not None:
        return Path(assets_dir) / "settings_0.8" / "settings_pigeon.svg"
    pigeon_root = Path(__file__).resolve().parents[3]
    return pigeon_root / "pigeonAssets" / "settings_0.8" / "settings_pigeon.svg"


def pigeon_focus_ring() -> tuple[str, ...]:
    return _PIGEON_FOCUS_RING


def normalize_pigeon_focus_id(focus_id: str) -> str:
    fid = str(focus_id or "").strip()
    return _FOCUS_ALIASES.get(fid, fid)


# ---------------------------------------------------------------------------
# Fonts / text
# ---------------------------------------------------------------------------


@lru_cache(maxsize=32)
def _load_font(size_px: int, *, bold: bool = True):
    from PIL import ImageFont

    from pigeon.font_paths import resolve_ui_font_extrabold, resolve_ui_font_medium

    path = resolve_ui_font_extrabold() if bold else resolve_ui_font_medium()
    if path:
        try:
            return ImageFont.truetype(str(path), size=max(8, int(size_px)))
        except OSError:
            pass
    return ImageFont.load_default()


@lru_cache(maxsize=128)
def _text_patch(
    text: str,
    size_px: int,
    fill_rgba: tuple[int, int, int, int],
    *,
    bold: bool = True,
) -> tuple[np.ndarray, int, int]:
    from PIL import Image, ImageDraw
    import cv2

    draw_text = str(text or "")
    if not draw_text:
        return np.zeros((1, 1, 4), dtype=np.uint8), 0, 0
    font = _load_font(size_px, bold=bold)
    probe = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    left, top, right, bottom = draw.textbbox((0, 0), draw_text, font=font)
    tw, th = max(1, right - left), max(1, bottom - top)
    pad = 1
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((pad - left, pad - top), draw_text, font=font, fill=fill_rgba)
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGBA2BGRA), tw + pad * 2, th + pad * 2


def _paste_bgra(dst: np.ndarray, patch: np.ndarray, x: int, y: int) -> None:
    if patch is None or patch.size == 0:
        return
    ph, pw = patch.shape[:2]
    x0, y0 = int(x), int(y)
    x1, y1 = x0 + pw, y0 + ph
    if x1 <= 0 or y1 <= 0 or x0 >= dst.shape[1] or y0 >= dst.shape[0]:
        return
    sx0 = max(0, -x0)
    sy0 = max(0, -y0)
    dx0 = max(0, x0)
    dy0 = max(0, y0)
    dx1 = min(dst.shape[1], x1)
    dy1 = min(dst.shape[0], y1)
    if dx0 >= dx1 or dy0 >= dy1:
        return
    src = patch[sy0 : sy0 + (dy1 - dy0), sx0 : sx0 + (dx1 - dx0)]
    if src.shape[2] < 4:
        dst[dy0:dy1, dx0:dx1, :3] = src[:, :, :3]
        dst[dy0:dy1, dx0:dx1, 3] = 255
        return
    a = src[:, :, 3:4].astype(np.float32) / 255.0
    out = dst[dy0:dy1, dx0:dx1].astype(np.float32)
    out[:, :, :3] = out[:, :, :3] * (1.0 - a) + src[:, :, :3].astype(np.float32) * a
    out[:, :, 3] = np.maximum(out[:, :, 3], src[:, :, 3].astype(np.float32))
    dst[dy0:dy1, dx0:dx1] = np.clip(out, 0, 255).astype(np.uint8)


def _paste_centered(dst: np.ndarray, patch: np.ndarray, cx: float, cy: float) -> None:
    if patch is None or patch.size == 0:
        return
    ph, pw = patch.shape[:2]
    _paste_bgra(dst, patch, int(round(cx - pw / 2.0)), int(round(cy - ph / 2.0)))


def _hex_to_bgr(hex_color: str) -> tuple[int, int, int]:
    s = str(hex_color or "").strip().lstrip("#")
    if len(s) >= 6:
        try:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
            return (b, g, r)
        except ValueError:
            pass
    return (0, 0, 255)


# ---------------------------------------------------------------------------
# Chrome drawing
# ---------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _rounded_rect_mask(w: int, h: int, radius: int) -> np.ndarray:
    """Opaque=255 mask for a rounded rectangle (tile / icon clip)."""
    from PIL import Image, ImageDraw

    ww, hh = max(1, int(w)), max(1, int(h))
    r = max(0, min(int(radius), ww // 2, hh // 2))
    img = Image.new("L", (ww, hh), 0)
    ImageDraw.Draw(img).rounded_rectangle((0, 0, ww - 1, hh - 1), radius=r, fill=255)
    return np.asarray(img, dtype=np.uint8)


def _apply_rounded_mask(cell: np.ndarray, radius: int) -> None:
    """Zero alpha outside the rounded tile so icons respect corner masks."""
    h, w = cell.shape[:2]
    mask = _rounded_rect_mask(w, h, radius)
    if cell.shape[2] >= 4:
        cell[:, :, 3] = np.minimum(cell[:, :, 3], mask)
    else:
        cell[mask == 0] = 0


def _draw_rounded_rect_outline(
    bgra: np.ndarray,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    radius: int,
    color_bgr: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    import cv2

    if w < 2 or h < 2:
        return
    t = max(1, int(thickness))
    outer = _rounded_rect_mask(w, h, radius)
    inner = np.zeros_like(outer)
    if w > 2 * t and h > 2 * t:
        inset = _rounded_rect_mask(w - 2 * t, h - 2 * t, max(0, int(radius) - t))
        inner[t : t + inset.shape[0], t : t + inset.shape[1]] = inset
    ring = cv2.subtract(outer, inner)
    ys, xs = np.where(ring > 0)
    if not ys.size:
        return
    yy = ys + int(y)
    xx = xs + int(x)
    valid = (
        (yy >= 0)
        & (yy < bgra.shape[0])
        & (xx >= 0)
        & (xx < bgra.shape[1])
    )
    yy, xx = yy[valid], xx[valid]
    bgra[yy, xx, 0] = color_bgr[0]
    bgra[yy, xx, 1] = color_bgr[1]
    bgra[yy, xx, 2] = color_bgr[2]
    bgra[yy, xx, 3] = 255


def _draw_rounded_rect_fill(
    bgra: np.ndarray,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    radius: int,
    color_bgr: tuple[int, int, int],
) -> None:
    if w < 2 or h < 2:
        return
    mask = _rounded_rect_mask(w, h, radius)
    y0, x0 = int(y), int(x)
    y1, x1 = y0 + h, x0 + w
    if y0 >= bgra.shape[0] or x0 >= bgra.shape[1] or y1 <= 0 or x1 <= 0:
        return
    sy0 = max(0, -y0)
    sx0 = max(0, -x0)
    dy0 = max(0, y0)
    dx0 = max(0, x0)
    dy1 = min(bgra.shape[0], y1)
    dx1 = min(bgra.shape[1], x1)
    m = mask[sy0 : sy0 + (dy1 - dy0), sx0 : sx0 + (dx1 - dx0)] > 0
    roi = bgra[dy0:dy1, dx0:dx1]
    roi[m, 0] = color_bgr[0]
    roi[m, 1] = color_bgr[1]
    roi[m, 2] = color_bgr[2]
    roi[m, 3] = 255


def _draw_corner_brackets(
    bgra: np.ndarray,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    length: int,
    color_bgr: tuple[int, int, int],
    thickness: int = 2,
) -> None:
    import cv2

    L = max(4, int(length))
    t = max(1, int(thickness))
    pts = [
        # TL
        ((x, y + L), (x, y), (x + L, y)),
        # TR
        ((x + w - 1 - L, y), (x + w - 1, y), (x + w - 1, y + L)),
        # BL
        ((x, y + h - 1 - L), (x, y + h - 1), (x + L, y + h - 1)),
        # BR
        ((x + w - 1 - L, y + h - 1), (x + w - 1, y + h - 1), (x + w - 1, y + h - 1 - L)),
    ]
    for a, b, c in pts:
        cv2.line(bgra, a, b, (*color_bgr, 255), t, lineType=cv2.LINE_AA)
        cv2.line(bgra, b, c, (*color_bgr, 255), t, lineType=cv2.LINE_AA)


def _draw_icon_general(tile: np.ndarray, color_bgr: tuple[int, int, int]) -> None:
    import cv2

    h, w = tile.shape[:2]
    cx, cy = w // 2, h // 2
    for i, dy in enumerate((-18, 0, 18)):
        y = cy + dy
        cv2.circle(tile, (cx - 22, y), 3, (*color_bgr, 255), -1, lineType=cv2.LINE_AA)
        cv2.line(
            tile,
            (cx - 12, y),
            (cx + 24, y),
            (*color_bgr, 255),
            3,
            lineType=cv2.LINE_AA,
        )


def _draw_icon_colors(tile: np.ndarray) -> None:
    """Rainbow square with black center disc."""
    import cv2

    h, w = tile.shape[:2]
    pad = 18
    x0, y0 = pad, pad
    bw, bh = w - 2 * pad, h - 2 * pad
    # HSV sweep left→right.
    for x in range(bw):
        hue = int(179 * x / max(1, bw - 1))
        hsv = np.uint8([[[hue, 255, 255]]])
        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
        tile[y0 : y0 + bh, x0 + x, 0] = int(bgr[0])
        tile[y0 : y0 + bh, x0 + x, 1] = int(bgr[1])
        tile[y0 : y0 + bh, x0 + x, 2] = int(bgr[2])
        tile[y0 : y0 + bh, x0 + x, 3] = 255
    cv2.circle(
        tile,
        (w // 2, h // 2),
        14,
        (0, 0, 0, 255),
        -1,
        lineType=cv2.LINE_AA,
    )


def _draw_icon_display(tile: np.ndarray, color_bgr: tuple[int, int, int]) -> None:
    import cv2

    h, w = tile.shape[:2]
    cx, cy = w // 2, h // 2
    rw, rh = 36, 56
    x0, y0 = cx - rw // 2, cy - rh // 2
    cv2.rectangle(
        tile,
        (x0, y0),
        (x0 + rw, y0 + rh),
        (*color_bgr, 255),
        2,
        lineType=cv2.LINE_AA,
    )
    cv2.circle(tile, (cx, y0 + 8), 2, (*color_bgr, 255), -1, lineType=cv2.LINE_AA)


def _draw_icon_update(tile: np.ndarray, color_bgr: tuple[int, int, int]) -> None:
    import cv2

    h, w = tile.shape[:2]
    cx, cy = w // 2, h // 2
    r = 22
    # Two open arcs with arrowheads.
    cv2.ellipse(
        tile,
        (cx, cy),
        (r, r),
        0,
        40,
        220,
        (*color_bgr, 255),
        3,
        lineType=cv2.LINE_AA,
    )
    cv2.ellipse(
        tile,
        (cx, cy),
        (r, r),
        0,
        220,
        400,
        (*color_bgr, 255),
        3,
        lineType=cv2.LINE_AA,
    )
    # Arrow tips
    cv2.arrowedLine(
        tile,
        (cx + r - 2, cy - 8),
        (cx + r + 2, cy + 6),
        (*color_bgr, 255),
        3,
        tipLength=0.45,
        line_type=cv2.LINE_AA,
    )
    cv2.arrowedLine(
        tile,
        (cx - r + 2, cy + 8),
        (cx - r - 2, cy - 6),
        (*color_bgr, 255),
        3,
        tipLength=0.45,
        line_type=cv2.LINE_AA,
    )


def _draw_icon_about(tile: np.ndarray, color_bgr: tuple[int, int, int]) -> None:
    import cv2

    h, w = tile.shape[:2]
    cx, cy = w // 2, h // 2
    cv2.line(
        tile,
        (cx - 26, cy),
        (cx + 26, cy),
        (*color_bgr, 255),
        4,
        lineType=cv2.LINE_AA,
    )
    cv2.circle(tile, (cx + 8, cy), 7, (*color_bgr, 255), -1, lineType=cv2.LINE_AA)
    cv2.circle(tile, (cx + 8, cy), 7, (0, 0, 0, 255), 2, lineType=cv2.LINE_AA)


def _draw_tile_icon(
    tile: np.ndarray,
    icon: str,
    color_bgr: tuple[int, int, int],
) -> None:
    if icon == "general":
        _draw_icon_general(tile, color_bgr)
    elif icon == "colors":
        _draw_icon_colors(tile)
    elif icon == "display":
        _draw_icon_display(tile, color_bgr)
    elif icon == "update":
        _draw_icon_update(tile, color_bgr)
    elif icon == "about":
        _draw_icon_about(tile, color_bgr)


def _draw_badge(
    bgra: np.ndarray,
    *,
    cx: int,
    cy: int,
    text: str,
    fill_bgr: tuple[int, int, int],
) -> None:
    import cv2

    r = 12
    cv2.circle(bgra, (cx, cy), r, (*fill_bgr, 255), -1, lineType=cv2.LINE_AA)
    patch, tw, th = _text_patch(text, 14, (255, 255, 255, 255), bold=True)
    _paste_bgra(bgra, patch, cx - tw // 2, cy - th // 2)


def _tile_origin(index: int) -> tuple[int, int]:
    col = index % _COLS
    row = index // _COLS
    x = _GRID_X + col * (_TILE + _GAP_X)
    y = _GRID_Y + row * (_TILE + _GAP_Y)
    return x, y


def apply_pigeon_settings_svg_state(root, state: MainSettingsState) -> None:
    """No-op — grid UI is drawn in Pillow/OpenCV, not from SVG layers."""
    del root, state


def render_pigeon_settings_bgra(
    state: MainSettingsState | None = None,
    *,
    svg_path: Path | str | None = None,
    assets_dir: Path | str | None = None,
) -> np.ndarray:
    del svg_path
    st = state if state is not None else MainSettingsState()
    focused = normalize_pigeon_focus_id(st.pigeon_focused_id)
    ui_hex = str(getattr(st.theme, "ui", "#ff0013") or "#ff0013")
    # Button / deselected swatch — solid fill for every tile face.
    fill_hex = str(getattr(st.theme, "deselected", "#202020") or "#202020")
    ui_bgr = _hex_to_bgr(ui_hex)
    fill_bgr = _hex_to_bgr(fill_hex)
    white = (255, 255, 255)

    out = np.zeros((DESIGN_H, DESIGN_W, 4), dtype=np.uint8)
    out[:, :, 3] = 255
    # Same slanted theme plate used by main / preferences / color settings.
    from pigeon.widgets.main_settings import _draw_container_background_bgra

    _draw_container_background_bgra(
        out,
        ui_hex=ui_hex,
        assets_dir=assets_dir,
    )

    # Header: SETTINGS / Pigeon (left), version meta (right).
    settings_p, sw, sh = _text_patch(
        "SETTINGS", _HEADER_SETTINGS_SIZE, (255, 255, 255, 255), bold=True
    )
    _paste_bgra(out, settings_p, _HEADER_X, _HEADER_SETTINGS_Y)
    pigeon_p, _pw, _ph = _text_patch(
        "Pigeon", _HEADER_PIGEON_SIZE, (220, 220, 220, 255), bold=False
    )
    _paste_bgra(out, pigeon_p, _HEADER_X, _HEADER_PIGEON_Y)

    ver = str(getattr(st, "version_string", "") or "").strip()
    if ver and not ver.lower().startswith("v"):
        meta = f"MAIN {ver}"
    elif ver:
        meta = f"MAIN {ver.lstrip('vV').lstrip('.')}"
    else:
        meta = "MAIN"
    meta_p, mw, _mh = _text_patch(
        meta, _HEADER_META_SIZE, (255, 255, 255, 255), bold=False
    )
    _paste_bgra(out, meta_p, DESIGN_W - _HEADER_X - mw, _HEADER_META_Y)

    # BACK affordance: underline SETTINGS when focused.
    if focused == "pigeon_back":
        import cv2

        y = _HEADER_SETTINGS_Y + sh + 4
        cv2.line(
            out,
            (_HEADER_X, y),
            (_HEADER_X + sw, y),
            (*ui_bgr, 255),
            3,
            lineType=cv2.LINE_AA,
        )

    for i, tile in enumerate(_TILES):
        x, y = _tile_origin(i)
        selected = focused == tile.focus_id
        face_bgr = ui_bgr if selected else fill_bgr
        icon_color = white
        if selected:
            label_rgba = (*ui_bgr[::-1], 255)  # BGR→RGB for Pillow fill
        else:
            label_rgba = (255, 255, 255, 255)

        # Cell buffer: filled rounded face + icon, then mask corners.
        cell = np.zeros((_TILE, _TILE, 4), dtype=np.uint8)
        _draw_rounded_rect_fill(
            cell,
            x=0,
            y=0,
            w=_TILE,
            h=_TILE,
            radius=_CORNER,
            color_bgr=face_bgr,
        )
        _draw_tile_icon(cell, tile.icon, icon_color)
        _apply_rounded_mask(cell, _CORNER)
        _paste_bgra(out, cell, x, y)

        border = ui_bgr if selected else white
        _draw_rounded_rect_outline(
            out,
            x=x,
            y=y,
            w=_TILE,
            h=_TILE,
            radius=_CORNER,
            color_bgr=border,
            thickness=_STROKE,
        )
        _draw_corner_brackets(
            out,
            x=x + 4,
            y=y + 4,
            w=_TILE - 8,
            h=_TILE - 8,
            length=_BRACKET,
            color_bgr=border,
            thickness=2,
        )

        # Update badge on ABOUT when an update is available.
        if tile.focus_id == "about_button" and bool(getattr(st, "update_available", False)):
            _draw_badge(
                out,
                cx=x + _TILE - 6,
                cy=y + 6,
                text="1",
                fill_bgr=ui_bgr,
            )

        lab_p, lw, lh = _text_patch(
            tile.label, _LABEL_SIZE, label_rgba, bold=True
        )
        _paste_bgra(
            out,
            lab_p,
            x + (_TILE - lw) // 2,
            y + _TILE + _LABEL_GAP,
        )

    return out


__all__ = [
    "apply_pigeon_settings_svg_state",
    "default_pigeon_settings_svg_path",
    "normalize_pigeon_focus_id",
    "pigeon_focus_ring",
    "render_pigeon_settings_bgra",
]
