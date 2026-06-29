"""
Standalone preview for the Pigeon 0.7 settings page (SVG menu + arrow-key focus).

From ``Pigeon_0.7.0_Development``::

    Pigeon_0.7.0/pigeonSystem/.venv/bin/python testingEnvironments/settings_page_test.py

Or double-click / run::

    testingEnvironments/run_settings_page_test.command

Shim::

    python -m pigeon.settings_page_test

Keys::

    Right — next (Location → Network → Device 1 → … → Exit → Location)
    Left — previous (reverse)
    Esc / q — quit
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path


def _ensure_pigeon_on_path() -> None:
    dev_root = Path(__file__).resolve().parents[1]
    system = dev_root / "Pigeon_0.7.0" / "pigeonSystem"
    s = str(system)
    if s not in sys.path:
        sys.path.insert(0, s)


_ensure_pigeon_on_path()

import cv2
import numpy as np
from PIL import Image, ImageTk

from pigeon.design import DESIGN_H, DESIGN_W
from pigeon.widgets.settings_page import (
    SettingsPageState,
    default_settings_svg_path,
    render_settings_page_bgra,
)

_NAV_ORDER_HINT = "Location → Network → Device 1 → Device 2 → Device 3 → Exit"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Preview the Pigeon 0.7 settings page SVG menu.")
    ap.add_argument(
        "--svg",
        type=Path,
        default=None,
        help="Path to settings_0.7.svg (default: Pigeon_GFX/pigeonAI/settings_0.7.svg).",
    )
    ap.add_argument(
        "--width",
        type=int,
        default=DESIGN_W,
        help=f"Window width (default {DESIGN_W}).",
    )
    ap.add_argument(
        "--height",
        type=int,
        default=DESIGN_H,
        help=f"Window height (default {DESIGN_H}).",
    )
    args = ap.parse_args(argv)

    svg_path = args.svg or default_settings_svg_path()
    if not svg_path.is_file():
        sys.stderr.write(f"pigeon: settings SVG not found: {svg_path}\n")
        return 1

    w = max(320, int(args.width))
    h = max(200, int(args.height))

    state = SettingsPageState()
    root = tk.Tk()
    root.title("Pigeon — settings page (test)")
    root.geometry(f"{w}x{h}")
    root.configure(bg="#000000")

    frame = tk.Frame(root, bg="#000000", bd=0, highlightthickness=0)
    frame.pack(fill=tk.BOTH, expand=True)

    label = tk.Label(frame, bd=0, highlightthickness=0, bg="#000000")
    label.pack(fill=tk.BOTH, expand=True)

    status = tk.StringVar(value="")
    status_bar = tk.Label(
        root,
        textvariable=status,
        anchor=tk.W,
        fg="#02e900",
        bg="#202020",
        font=("Helvetica", 11),
        padx=8,
        pady=4,
    )
    status_bar.pack(fill=tk.X, side=tk.BOTTOM)

    photo_holder: list[ImageTk.PhotoImage | None] = [None]

    def _update_status() -> None:
        idx = int(state.selected)
        status.set(
            f"Selected: {state.navigation_label} ({idx + 1}/6)  |  "
            f"→ next  ← prev  |  {_NAV_ORDER_HINT}  |  Esc/q quit"
        )

    def _redraw() -> None:
        bgra = render_settings_page_bgra(state, svg_path=svg_path)
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        if bgr.shape[1] != w or bgr.shape[0] != h:
            bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_NEAREST)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        photo_holder[0] = ImageTk.PhotoImage(image=pil)
        label.configure(image=photo_holder[0])
        _update_status()

    def _on_left(_event: tk.Event | None = None) -> None:
        state.advance(forward=False)
        _redraw()

    def _on_right(_event: tk.Event | None = None) -> None:
        state.advance(forward=True)
        _redraw()

    root.bind("<Left>", _on_left)
    root.bind("<Right>", _on_right)
    root.bind("<Escape>", lambda _e: root.destroy())
    root.bind("<q>", lambda _e: root.destroy())

    _redraw()
    print(
        "Pigeon settings page test — Left/Right to move selection. SVG:",
        svg_path,
        file=sys.stderr,
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
