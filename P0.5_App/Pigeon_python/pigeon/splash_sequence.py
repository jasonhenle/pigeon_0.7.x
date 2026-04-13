"""Launch splash: PNG sequence (e.g. 800×400 RGBA) under ``pigeonAssets/P_0.5_WIDGET_splash``.

Alpha is preserved end-to-end: the overlay is a full-``shell`` layer above ``content_host``,
so transparent pixels show the composited scene underneath. The last ``SPLASH_FADE_OUT_FRAMES``
frames apply an extra global alpha ramp so the whole graphic eases out smoothly.
"""

from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Folder name next to other pigeonAssets media (user-provided sequence).
SPLASH_SEQUENCE_DIRNAME = "P_0.5_WIDGET_splash"
SPLASH_NOMINAL_W = 800
SPLASH_NOMINAL_H = 400
SPLASH_FPS = 30
# Hard cap so a huge folder cannot block startup for minutes.
SPLASH_MAX_DURATION_S = 18.0
# Last N frames: global alpha ramps 1 → 0 so the splash (and any opaque art) eases out smoothly.
SPLASH_FADE_OUT_FRAMES = 21

# Built-in sequence when ``P_0.5_WIDGET_splash`` has no PNGs (same nominal size as the window).
FALLBACK_SPLASH_FRAME_COUNT = 72
_FALLBACK_SPLASH_INTRO_FRAMES = 12


def _natural_png_sort_key(p: Path) -> tuple[object, ...]:
    parts = [int(x) for x in re.findall(r"\d+", p.stem)]
    return tuple(parts) + (p.stem.lower(), p.name.lower())


def list_splash_png_paths(assets_root: Path) -> list[Path]:
    """Sorted ``*.png`` paths under ``assets_root / SPLASH_SEQUENCE_DIRNAME``, or empty if missing."""
    d = assets_root / SPLASH_SEQUENCE_DIRNAME
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    return sorted(files, key=_natural_png_sort_key)


def load_splash_bgra(path: Path) -> np.ndarray | None:
    """BGRA uint8, or None. Adds opaque alpha if the file has no alpha channel."""
    im = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if im is None or im.size == 0:
        return None
    if im.ndim != 3:
        return None
    if im.shape[2] == 3:
        bgra = cv2.cvtColor(im, cv2.COLOR_BGR2BGRA)
        bgra[:, :, 3] = 255
        return bgra
    if im.shape[2] == 4:
        return im
    return None


def composite_splash_over_bg(bgra: np.ndarray, bg_bgr: tuple[int, int, int]) -> np.ndarray:
    """Alpha-composite BGRA over a solid BGR background (same H×W)."""
    if bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError("expected BGRA")
    h, w = bgra.shape[:2]
    base = np.empty((h, w, 3), dtype=np.uint8)
    base[:, :] = bg_bgr
    a = bgra[:, :, 3:4].astype(np.float32) / 255.0
    fg = bgra[:, :, :3].astype(np.float32)
    bg = base.astype(np.float32)
    out = fg * a + bg * (1.0 - a)
    return np.clip(out, 0.0, 255.0).astype(np.uint8)


def apply_splash_global_alpha(bgra: np.ndarray, factor: float) -> np.ndarray:
    """Multiply the alpha channel by ``factor`` in ``[0, 1]`` (copy)."""
    if bgra.ndim != 3 or bgra.shape[2] != 4:
        return bgra
    f = max(0.0, min(1.0, float(factor)))
    if f >= 0.999:
        return bgra
    out = bgra.copy()
    out[:, :, 3] = np.clip(out[:, :, 3].astype(np.float32) * f, 0.0, 255.0).astype(np.uint8)
    return out


def builtin_splash_bgra_frame(
    frame_index: int,
    total_frames: int,
    *,
    width: int | None = None,
    height: int | None = None,
) -> np.ndarray:
    """
    Single RGBA frame as BGRA uint8 for the Tk overlay (no disk assets).

    Dark plate + wordmark; intro ramp and tail fade match the PNG path behavior.
    """
    w = int(width or SPLASH_NOMINAL_W)
    h = int(height or SPLASH_NOMINAL_H)
    w = max(64, w)
    h = max(32, h)
    # Intro ramp only; ``pigeon_0_5`` applies ``splash_end_fade_factor`` + ``apply_splash_global_alpha`` like PNGs.
    intro = min(
        1.0,
        (float(frame_index) + 1.0) / float(max(1, _FALLBACK_SPLASH_INTRO_FRAMES)),
    )
    alpha_i = int(round(255.0 * max(intro, 0.12)))
    _ = total_frames  # frame count matches PNG path timing / fade window

    img = Image.new("RGBA", (w, h), (8, 8, 10, alpha_i))
    draw = ImageDraw.Draw(img)
    title = "Pigeon"
    sub = "0.5"
    title_px = max(18, h // 5)
    sub_px = max(11, h // 12)
    font_title = ImageFont.load_default()
    font_sub = ImageFont.load_default()
    for path, px in (
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", title_px),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", title_px),
        ("/Library/Fonts/Arial Bold.ttf", title_px),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", title_px),
    ):
        try:
            font_title = ImageFont.truetype(path, px)
            break
        except OSError:
            continue
    for path, px in (
        ("/System/Library/Fonts/Supplemental/Arial.ttf", sub_px),
        ("/Library/Fonts/Arial.ttf", sub_px),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sub_px),
    ):
        try:
            font_sub = ImageFont.truetype(path, px)
            break
        except OSError:
            continue

    tb = draw.textbbox((0, 0), title, font=font_title)
    sb = draw.textbbox((0, 0), sub, font=font_sub)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    sw, sh = sb[2] - sb[0], sb[3] - sb[1]
    gap = max(4, h // 40)
    block_h = th + gap + sh
    y0 = (h - block_h) // 2
    tx = (w - tw) // 2
    sx = (w - sw) // 2
    fill = (245, 247, 250, alpha_i)
    draw.text((tx, y0), title, font=font_title, fill=fill)
    draw.text((sx, y0 + th + gap), sub, font=font_sub, fill=fill)

    rgba = np.asarray(img)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def splash_end_fade_factor(frame_index: int, total_frames: int, fade_frames: int) -> float:
    """
    Return a multiplier for ``apply_splash_global_alpha``: 1.0 until the fade zone, then linear 1 → 0.
    ``frame_index`` is 0-based for the frame currently being shown.
    """
    if total_frames <= 0 or fade_frames <= 0:
        return 1.0
    ff = min(fade_frames, total_frames)
    start = total_frames - ff
    if frame_index < start:
        return 1.0
    if ff <= 1:
        return 0.0 if frame_index >= start else 1.0
    u = (frame_index - start) / float(ff - 1)
    return max(0.0, 1.0 - u)


def bgra_to_pil_rgba(bgra: np.ndarray) -> Image.Image:
    """BGRA uint8 → PIL RGBA (for Tk PhotoImage with per-pixel alpha)."""
    if bgra.ndim != 3 or bgra.shape[2] != 4:
        raise ValueError("expected BGRA")
    rgba = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
    return Image.fromarray(rgba, "RGBA")
