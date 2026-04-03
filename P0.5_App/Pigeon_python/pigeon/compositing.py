"""Image blending and scaling helpers."""

from __future__ import annotations

import cv2
import numpy as np


def scale_bgra_rgb(bgra: np.ndarray, rgb_factor: float) -> np.ndarray:
    """Scale BGR channels only; alpha unchanged. Used for idle-dim per-widget tuning."""
    f = float(rgb_factor)
    if f >= 0.999:
        return bgra
    if f <= 0.0:
        out = np.zeros_like(bgra)
        out[:, :, 3] = bgra[:, :, 3]
        return out
    out = bgra.copy()
    out[:, :, :3] = np.clip(bgra[:, :, :3].astype(np.float32) * f, 0, 255).astype(np.uint8)
    return out


def alpha_blend_bgra_over_bgr(base_bgr: np.ndarray, overlay_bgra: np.ndarray) -> np.ndarray:
    if base_bgr.shape[:2] != overlay_bgra.shape[:2]:
        raise ValueError("Overlay and base frame sizes must match")

    overlay_bgr = overlay_bgra[:, :, :3].astype(np.float32)
    alpha = overlay_bgra[:, :, 3:4].astype(np.float32) / 255.0
    base = base_bgr.astype(np.float32)
    out = overlay_bgr * alpha + base * (1.0 - alpha)
    return np.clip(out, 0, 255).astype(np.uint8)


def scale_height_and_center_crop(image: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Scale by height to target_h, then center-crop horizontally to target_w."""
    src_h, src_w = image.shape[:2]
    scale = target_h / float(src_h)
    scaled_w = int(round(src_w * scale))
    resized = cv2.resize(image, (scaled_w, target_h), interpolation=cv2.INTER_AREA)

    if scaled_w < target_w:
        pad = target_w - scaled_w
        left = pad // 2
        right = pad - left
        if resized.ndim == 3 and resized.shape[2] == 4:
            pad_value = (0, 0, 0, 0)
        else:
            pad_value = (0, 0, 0)
        return cv2.copyMakeBorder(
            resized,
            top=0,
            bottom=0,
            left=left,
            right=right,
            borderType=cv2.BORDER_CONSTANT,
            value=pad_value,
        )

    x0 = (scaled_w - target_w) // 2
    x1 = x0 + target_w
    return resized[:, x0:x1]
