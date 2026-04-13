"""Launch splash: PNG sequence (e.g. 800×400 RGBA) under ``pigeonAssets/P_0.5_WIDGET_splash``."""

from __future__ import annotations

import re
from pathlib import Path

import cv2
import numpy as np

# Folder name next to other pigeonAssets media (user-provided sequence).
SPLASH_SEQUENCE_DIRNAME = "P_0.5_WIDGET_splash"
SPLASH_NOMINAL_W = 800
SPLASH_NOMINAL_H = 400
SPLASH_FPS = 30
# Hard cap so a huge folder cannot block startup for minutes.
SPLASH_MAX_DURATION_S = 18.0


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
