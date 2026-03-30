"""Grid overlay drawing at design resolution (shared by main UI and widget shells)."""

from __future__ import annotations

import cv2
import numpy as np


def build_grid_overlay_bgra(width: int, height: int, rows: int, cols: int) -> np.ndarray:
    """BGRA overlay: rows×cols square cells, centered in width×height, labels [row,col] 1-based."""
    overlay = np.zeros((height, width, 4), dtype=np.uint8)

    cell = min(width // cols, height // rows)
    grid_w = cell * cols
    grid_h = cell * rows
    x0 = (width - grid_w) // 2
    y0 = (height - grid_h) // 2

    line_color = (255, 255, 0, 235)
    text_color = (0, 255, 255, 245)
    text_outline_color = (0, 0, 0, 255)

    cv2.rectangle(overlay, (x0, y0), (x0 + grid_w, y0 + grid_h), line_color, 4, lineType=cv2.LINE_AA)

    for c in range(1, cols):
        x = x0 + c * cell
        cv2.line(overlay, (x, y0), (x, y0 + grid_h), line_color, 3, lineType=cv2.LINE_AA)
    for r in range(1, rows):
        y = y0 + r * cell
        cv2.line(overlay, (x0, y), (x0 + grid_w, y), line_color, 3, lineType=cv2.LINE_AA)

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.55, cell / 120.0)
    thickness = 2
    for r in range(rows):
        for c in range(cols):
            label = f"[{r + 1},{c + 1}]"
            (tw, th), baseline = cv2.getTextSize(label, font, font_scale, thickness)
            cx = x0 + c * cell + (cell // 2)
            cy = y0 + r * cell + (cell // 2)
            tx = cx - (tw // 2)
            ty = cy + (th // 2)

            pad_x = max(4, int(cell * 0.04))
            pad_y = max(3, int(cell * 0.03))
            box_x1 = tx - pad_x
            box_y1 = ty - th - pad_y
            box_x2 = tx + tw + pad_x
            box_y2 = ty + baseline + pad_y
            cv2.rectangle(
                overlay,
                (box_x1, box_y1),
                (box_x2, box_y2),
                (0, 0, 0, 165),
                thickness=-1,
                lineType=cv2.LINE_AA,
            )
            cv2.putText(
                overlay,
                label,
                (tx, ty),
                font,
                font_scale,
                text_outline_color,
                thickness + 2,
                lineType=cv2.LINE_AA,
            )
            cv2.putText(overlay, label, (tx, ty), font, font_scale, text_color, thickness, lineType=cv2.LINE_AA)

    return overlay
