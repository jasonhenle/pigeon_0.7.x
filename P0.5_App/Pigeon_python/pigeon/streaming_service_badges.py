"""Map Apple TV foreground streaming app (bundle id / name) to assets in ``pigeonAssets``."""

from __future__ import annotations

from pathlib import Path

# Order: first matching rule wins.
# ``bundle_contains`` / ``name_contains`` are matched case-insensitively (substring).
# ``asset_names``: first existing file under ``assets_dir`` wins (PNG or JPEG).
_RULES: tuple[tuple[str | None, str | None, tuple[str, ...], str], ...] = (
    ("disney.disneyplus", None, ("DisneyPluslogo.png", "Disney Plus logo white text.png", "disneyPlusLogo.png"), "Disney+"),
    (None, "disney+", ("DisneyPluslogo.png", "Disney Plus logo white text.png", "disneyPlusLogo.png"), "Disney+"),
    (None, "disney", ("DisneyPluslogo.png", "Disney Plus logo white text.png", "disneyPlusLogo.png"), "Disney+"),
    ("netflix", None, ("Netflix_Logo_RGB.png",), "Netflix"),
    (None, "netflix", ("Netflix_Logo_RGB.png",), "Netflix"),
    ("tvwatch", None, ("Apple_TV_Plus_logo.png",), "Apple TV"),
    (None, "apple tv", ("Apple_TV_Plus_logo.png",), "Apple TV"),
    ("peacock", None, ("peacockLogo.jpg", "peacockLogo.png"), "Peacock"),
    (None, "peacock", ("peacockLogo.jpg", "peacockLogo.png"), "Peacock"),
)


def _first_existing_asset(root: Path, basenames: tuple[str, ...]) -> str | None:
    for name in basenames:
        p = root / name
        if p.is_file():
            return name
    return None


def resolve_streaming_badge_media(
    assets_dir: str | Path,
    *,
    app_name: str,
    app_id: str,
) -> tuple[str | None, str]:
    """
    Return ``(asset_basename_or_none, display_name)`` for the streaming service.

    If no rule matches, ``display_name`` falls back to the app’s display name from the TV.
    """
    root = Path(assets_dir)
    bid = (app_id or "").strip().lower()
    an = (app_name or "").strip().lower()

    for bfrag, nfrag, candidates, display in _RULES:
        bundle_hit = bool(bfrag and bfrag in bid)
        name_hit = bool(nfrag and nfrag in an)
        if not bundle_hit and not name_hit:
            continue
        fn = _first_existing_asset(root, candidates)
        if fn:
            return fn, display

    raw_name = (app_name or "").strip()
    if raw_name:
        return None, raw_name
    if bid:
        leaf = bid.rsplit(".", 1)[-1].replace("_", " ")
        if leaf:
            return None, leaf[:40]
    return None, "Playing"


def resolve_streaming_badge_filename(
    assets_dir: str | Path,
    *,
    app_name: str,
    app_id: str,
) -> str | None:
    """Backward-compatible: first matching logo/JPEG basename, or ``None``."""
    fn, _ = resolve_streaming_badge_media(
        assets_dir, app_name=app_name, app_id=app_id
    )
    return fn
