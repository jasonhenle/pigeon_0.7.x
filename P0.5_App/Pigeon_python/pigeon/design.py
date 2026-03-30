"""Canonical 2.39:1 design resolution and 19×8 grid math (5126×2160).

Matches P0.5 docs: 152 boxes (19 variable × 8 fixed), square cells centered on canvas.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hypothetical maximum Pigeon design canvas (matches your asset pipeline).
DESIGN_W = 5126
DESIGN_H = 2160

GRID_COLS = 19
GRID_ROWS = 8


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
    row_1based: int,
    col_1based: int,
    width: int = DESIGN_W,
    height: int = DESIGN_H,
    cols: int = GRID_COLS,
    rows: int = GRID_ROWS,
) -> tuple[int, int, int, int]:
    """
    Pixel rectangle (x, y, w, h) for a widget whose **top-left** sits on grid cell [row, col] (1-based).

    squares_wide / squares_tall = span in grid cells.
    """
    g = get_grid_geometry(width=width, height=height, cols=cols, rows=rows)
    x = g.x0 + (col_1based - 1) * g.cell
    y = g.y0 + (row_1based - 1) * g.cell
    w = squares_wide * g.cell
    h = squares_tall * g.cell
    return (x, y, w, h)


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
