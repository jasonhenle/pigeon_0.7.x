"""Canonical 2.39:1 design resolution and 19×8 grid math (5126×2160).

Matches P0.5 docs: 152 boxes (19 variable × 8 fixed), square cells centered on canvas.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Hypothetical maximum Pigeon design canvas (matches your asset pipeline).
DESIGN_W = 5126
DESIGN_H = 2160

GRID_COLS = 19
GRID_ROWS = 8

# Cached (design-sized) full-width gradient for rows 6–8 chrome.
_playback_lower_gradient: tuple[int, int, int, int, np.ndarray] | None = None


@dataclass(frozen=True)
class GridGeometry:
    cell: int
    x0: int
    y0: int
    grid_w: int
    grid_h: int
    cols: int
    rows: int


def get_grid_geometry(
    *,
    width: int = DESIGN_W,
    height: int = DESIGN_H,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> GridGeometry:
    """Square cells, grid centered in the canvas (may leave blank margin on sides or top/bottom)."""
    cell = min(width // cols, height // rows)
    grid_w = cell * cols
    grid_h = cell * rows
    x0 = (width - grid_w) // 2
    y0 = (height - grid_h) // 2
    return GridGeometry(
        cell=cell,
        x0=x0,
        y0=y0,
        grid_w=grid_w,
        grid_h=grid_h,
        cols=cols,
        rows=rows,
    )


def rect_for_span_at_cell(
    squares_wide: int,
    squares_tall: int,
    *,
    row_1based: int | float,
    col_1based: int,
    width: int = DESIGN_W,
    height: int = DESIGN_H,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> tuple[int, int, int, int]:
    """
    Pixel rectangle (x, y, w, h) for a widget whose **top-left** sits on grid cell [row, col] (1-based).

    squares_wide / squares_tall = span in grid cells.
    ``row_1based`` may be fractional (e.g. 6.5 → half a cell higher than row 7).
    """
    g = get_grid_geometry(width=width, height=height, cols=cols, rows=rows)
    x = g.x0 + (col_1based - 1) * g.cell
    y = int(round(g.y0 + (float(row_1based) - 1.0) * g.cell))
    w = squares_wide * g.cell
    h = squares_tall * g.cell
    y = max(0, min(y, height - h))
    x = max(0, min(x, width - w))
    return (x, y, w, h)


def rect_for_span_top_right_at_cell(
    squares_wide: int,
    squares_tall: int,
    *,
    row_1based: int | float,
    col_right_1based: float,
    width: int = DESIGN_W,
    height: int = DESIGN_H,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> tuple[int, int, int, int]:
    """
    Pixel rectangle (x, y, w, h) whose **top-right** aligns to grid coordinate (row, col_right_1based).

    ``col_right_1based`` may be fractional (e.g. 15.5 → half-cell); x = x0 + (col - 1) * cell - w.
    ``row_1based`` may be fractional (half-cell vertical nudge).
    """
    g = get_grid_geometry(width=width, height=height, cols=cols, rows=rows)
    x_right = g.x0 + (float(col_right_1based) - 1.0) * g.cell
    w = squares_wide * g.cell
    h = squares_tall * g.cell
    x = int(round(x_right - w))
    y = int(round(g.y0 + (float(row_1based) - 1.0) * g.cell))
    x = max(0, min(x, width - w))
    y = max(0, min(y, height - h))
    return (x, y, w, h)


def playback_lower_gradient_bgra(
    *,
    width: int = DESIGN_W,
    height: int = DESIGN_H,
    row_top_1based: int = 6,
    row_bottom_1based: int = 8,
    bottom_opacity: float = 0.7,
) -> tuple[int, int, int, int, np.ndarray]:
    """
    Full-width black gradient over grid rows ``row_top_1based`` … ``row_bottom_1based`` (inclusive).

    Top of row 6: fully transparent. Bottom of row 8: ``bottom_opacity`` black (e.g. 0.7 → 70% opaque).
    Returns ``(x, y, w, h, bgra)`` with ``x=0``, ``w=width`` (entire design canvas width).
    """
    global _playback_lower_gradient
    if _playback_lower_gradient is not None:
        return _playback_lower_gradient

    g = get_grid_geometry(width=width, height=height)
    y_top = g.y0 + (row_top_1based - 1) * g.cell
    y_below_bottom = g.y0 + row_bottom_1based * g.cell
    h_grad = max(1, y_below_bottom - y_top)
    patch = np.zeros((h_grad, width, 4), dtype=np.uint8)
    t = np.linspace(0.0, 1.0, h_grad, dtype=np.float32)
    a = np.round(t * float(bottom_opacity) * 255.0).clip(0, 255).astype(np.uint8)
    patch[:, :, 3] = a[:, np.newaxis]
    _playback_lower_gradient = (0, y_top, width, h_grad, patch)
    return _playback_lower_gradient


def rect_for_span_from_origin(
    squares_wide: int,
    squares_tall: int,
    *,
    width: int = DESIGN_W,
    height: int = DESIGN_H,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> tuple[int, int, int, int]:
    """
    Pixel rectangle (x, y, w, h) for a widget anchored at grid cell [1, 1] (1-based).

    squares_wide = columns spanned, squares_tall = rows spanned (e.g. 6×3 → wide=6, tall=3).
    """
    return rect_for_span_at_cell(
        squares_wide,
        squares_tall,
        row_1based=1,
        col_1based=1,
        width=width,
        height=height,
        cols=cols,
        rows=rows,
    )
