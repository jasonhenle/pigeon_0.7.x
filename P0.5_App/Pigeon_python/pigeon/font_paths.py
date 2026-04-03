"""Resolve Sharp Sans Bold / Medium (or overrides) for widget text."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_ui_font_medium() -> str | None:
    """
    Return path to Sharp Sans Medium, or None to fall back elsewhere.

    Set PIGEON_FONT_MEDIUM to a .ttf/.otf/.ttc path to override.
    """
    env = os.environ.get("PIGEON_FONT_MEDIUM")
    if env and Path(env).is_file():
        return env

    roots = [
        Path.home() / "Library" / "Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    ]
    globs = (
        "*Sharp*Sans*Medium*.ttf",
        "*Sharp*Sans*Medium*.otf",
        "*SharpSans*Medium*.ttf",
        "*SharpSans*Medium*.otf",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in globs:
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    return str(p)

    # Often only one "Sharp Sans" file is installed; avoid Bold if possible via weight in name
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.glob("*Sharp*Sans*.otf")) + sorted(root.glob("*Sharp*Sans*.ttf")):
            if p.is_file() and "bold" not in p.name.lower():
                return str(p)
    return None


def resolve_ui_font_extrabold() -> str | None:
    """
    Return path to Sharp Sans ExtraBold, or None to fall back to Bold / Medium.

    Set PIGEON_FONT_EXTRABOLD to a .ttf/.otf path to override.
    """
    env = os.environ.get("PIGEON_FONT_EXTRABOLD")
    if env and Path(env).is_file():
        return env

    roots = [
        Path.home() / "Library" / "Fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("/System/Library/Fonts/Supplemental"),
    ]
    globs = (
        "*Sharp*Sans*Extra*Bold*.otf",
        "*Sharp*Sans*Extra*Bold*.ttf",
        "*SharpSans*Extra*Bold*.otf",
        "*SharpSans*Extra*Bold*.ttf",
        "*Sharp*Sans*ExtraBold*.otf",
        "*Sharp*Sans*ExtraBold*.ttf",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in globs:
            for p in sorted(root.glob(pattern)):
                if p.is_file():
                    return str(p)
    return None


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
