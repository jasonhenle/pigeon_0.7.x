"""Resolve Sharp Sans Bold (or override) for widget text."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_ui_font_bold() -> str | None:
    """
    Return path to Sharp Sans Bold, or None to use Pillow fallback.

    Set PIGEON_FONT to a .ttf/.otf/.ttc path to override.
    """
    env = os.environ.get("PIGEON_FONT")
    if env and Path(env).is_file():
        return env

    roots = [
        Path.home() / "Library" / "Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    ]
    # Typical retail / Adobe naming patterns
    globs = (
        "*Sharp*Sans*Bold*.ttf",
        "*Sharp*Sans*Bold*.otf",
        "*SharpSans*Bold*.ttf",
        "*SharpSans*Bold*.otf",
        "*Sharp Sans*.ttf",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in globs:
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    return str(p)

    # macOS common bold sans fallbacks (not Sharp Sans, but readable)
    fallbacks = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
    ]
    for p in fallbacks:
        if p.is_file():
            return str(p)
    return None
