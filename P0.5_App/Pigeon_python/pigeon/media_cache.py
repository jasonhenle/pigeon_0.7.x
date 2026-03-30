"""
Cached assets in pigeonReFormattedMedia: ``{Title}_{PosterArt|Logo|Backdrop}.{ext}``.

Title is sanitized (same rules as poster export filenames).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pigeon.media_folders import ensure_reformatted_media_dir, pigeon_reformatted_media_dir
from pigeon.widgets.poster_art import sanitize_reformatted_poster_title

ASSET_POSTER_ART = "PosterArt"
ASSET_LOGO = "Logo"
ASSET_BACKDROP = "Backdrop"

_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def title_key(display_title: str) -> str:
    return sanitize_reformatted_poster_title(display_title) or "unknown"


def find_cached_reformatted_asset(title_key_str: str, asset_type: str) -> Path | None:
    """Return first existing file ``{title_key}_{asset_type}{ext}`` or None."""
    d = pigeon_reformatted_media_dir()
    for ext in _EXTS:
        p = d / f"{title_key_str}_{asset_type}{ext}"
        if p.is_file():
            return p
    return None


def copy_pulled_to_reformatted(src: Path, title_key_str: str, asset_type: str) -> Path:
    """Copy ``src`` to reformatted folder with ``{title}_{asset}{ext}``."""
    ensure_reformatted_media_dir()
    ext = src.suffix.lower()
    if ext not in _EXTS:
        ext = ".jpg"
    dest = pigeon_reformatted_media_dir() / f"{title_key_str}_{asset_type}{ext}"
    shutil.copy2(src, dest)
    return dest
