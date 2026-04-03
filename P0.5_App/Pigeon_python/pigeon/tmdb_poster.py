"""
TMDb: search movie and/or TV by title, download poster into pigeonPulledMedia, then poster pipeline.

Credentials (never commit real keys):
  - PIGEON_TMDB_READ_TOKEN  — JWT read access token (Bearer), preferred
  - PIGEON_TMDB_API_KEY     — v3 API key (query param)
  Or files in ~/.pigeon_0_5/: tmdb_read_token, tmdb_api_key (single line each)

Query hints (optional):
  - Prefix ``tv `` to search TV only (e.g. ``tv Breaking Bad``).
  - Prefix ``movie `` to search movies only.

This product uses the TMDb API but is not endorsed or certified by TMDb.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

import numpy as np

from pigeon.app_state import auto_delete_pulled_media
from pigeon.image_ui_protocol import backdrop_master_bgr_from_file, pulled_path_is_under_pulled_dir
from pigeon.media_cache import (
    ASSET_BACKDROP,
    ASSET_LOGO,
    ASSET_LOGO_EN,
    ASSET_POSTER_ART,
    copy_pulled_to_reformatted,
    find_cached_reformatted_asset,
    title_key,
)
from pigeon.media_folders import pigeon_pulled_media_dir

TMDB_API_BASE = "https://api.themoviedb.org/3"
POSTER_SIZE = "w780"  # good balance before local 1800-wide pipeline
IMG_BASE = f"https://image.tmdb.org/t/p/{POSTER_SIZE}"
LOGO_SIZE = "w500"
BACKDROP_SIZE = "w1280"
IMG_LOGO_BASE = f"https://image.tmdb.org/t/p/{LOGO_SIZE}"
IMG_BACKDROP_BASE = f"https://image.tmdb.org/t/p/{BACKDROP_SIZE}"

MediaKind = Literal["movie", "tv"]
Prefer = Literal["auto", "movie", "tv"]

_STATE_DIR = Path.home() / ".pigeon_0_5"
_UA = "Pigeon0.5/1.0 (local; +https://www.themoviedb.org/documentation/api)"


def _norm_query(s: str) -> str:
    """Normalize a query/title for substring + token matching (ASCII-ish, punctuation-insensitive)."""
    s = (s or "").strip().lower()
    if not s:
        return ""
    out: list[str] = []
    prev_space = False
    for ch in s:
        # Keep letters/digits; treat everything else as a space.
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        else:
            if not prev_space:
                out.append(" ")
                prev_space = True
    return " ".join("".join(out).split())


def _result_titles(item: dict) -> list[str]:
    """Candidate title strings for movie/tv result dicts."""
    vals = [
        item.get("title"),
        item.get("original_title"),
        item.get("name"),
        item.get("original_name"),
    ]
    out: list[str] = []
    for v in vals:
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    # de-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _tokens_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True if ``needle`` appears as consecutive tokens in ``haystack``."""
    if not needle:
        return True
    nlen = len(needle)
    for i in range(len(haystack) - nlen + 1):
        if haystack[i : i + nlen] == needle:
            return True
    return False


def _match_rank(query: str, item: dict) -> tuple[int, int]:
    """
    Sort key (tier, tie_break) for picking the best TMDb search hit — lexicographic **max** wins.
    ``tie_break`` is ``-len(normalized_title)`` so **shorter** titles win when tier ties
    (e.g. "Luck" over "Good Luck, Have Fun, Don't Die" for query ``luck``).

    Tiers (highest first):
      5 — normalized title **equals** query (exact)
      4 — multi-word query appears as **consecutive** whole tokens in the title
      3 — every query token appears as a **whole word** in the title
      2 — normalized query substring of normalized title (fuzzy)
      1 — every query token appears as substring in some title token
      0 — no match
    """
    nq = _norm_query(query)
    if not nq:
        return (0, 0)
    q_tokens = [t for t in nq.split() if t]
    if not q_tokens:
        return (0, 0)

    best: tuple[int, int] = (0, -(10**9))
    for title in _result_titles(item):
        nt = _norm_query(title)
        if not nt:
            continue
        t_tokens = [t for t in nt.split() if t]
        t_set = set(t_tokens)
        neg_len = -len(nt)

        tier = 0
        if nt == nq:
            tier = 5
        elif len(q_tokens) >= 2 and _tokens_subsequence(q_tokens, t_tokens):
            tier = 4
        elif all(qt in t_set for qt in q_tokens):
            tier = 3
        elif nq in nt:
            tier = 2
        elif all(any(qt in tw for tw in t_tokens) for qt in q_tokens):
            tier = 1

        cand = (tier, neg_len)
        if cand > best:
            best = cand
    return best


def load_tmdb_api_key() -> str | None:
    k = os.environ.get("PIGEON_TMDB_API_KEY", "").strip()
    if k:
        return k
    p = _STATE_DIR / "tmdb_api_key"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def load_tmdb_read_token() -> str | None:
    t = os.environ.get("PIGEON_TMDB_READ_TOKEN", "").strip()
    if t:
        return t
    p = _STATE_DIR / "tmdb_read_token"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip() or None
    return None


def _request_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    token = load_tmdb_read_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        api_key = load_tmdb_api_key()
        if not api_key:
            raise RuntimeError(
                "TMDb not configured. Set PIGEON_TMDB_READ_TOKEN or PIGEON_TMDB_API_KEY, "
                "or create ~/.pigeon_0_5/tmdb_read_token or tmdb_api_key (single line)."
            )
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({'api_key': api_key})}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})

    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_binary(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)


def _best_with_poster_from_results(query: str, results: list) -> dict | None:
    if not isinstance(results, list) or not results:
        return None
    with_poster = [r for r in results if isinstance(r, dict) and r.get("poster_path")]
    if not with_poster:
        return None
    # Prefer exact / whole-word title matches from the real search query; tie-break by
    # shorter title, then TMDb popularity. Fall back to full pool only if every rank is 0.
    scored = [(r, _match_rank(query, r)) for r in with_poster]
    best_key = max(rank for _, rank in scored)
    pool = [r for r, rank in scored if rank == best_key] if best_key[0] > 0 else with_poster
    return max(pool, key=lambda r: float(r.get("popularity") or 0.0))


def _best_from_results(query: str, results: list) -> dict | None:
    """Highest popularity among dict results (no poster requirement)."""
    if not isinstance(results, list) or not results:
        return None
    items = [r for r in results if isinstance(r, dict)]
    if not items:
        return None
    scored = [(r, _match_rank(query, r)) for r in items]
    best_key = max(rank for _, rank in scored)
    pool = [r for r, rank in scored if rank == best_key] if best_key[0] > 0 else items
    return max(pool, key=lambda r: float(r.get("popularity") or 0.0))


def search_movie_best_with_poster(query: str) -> dict | None:
    """Return one TMDb movie dict (has poster_path) or None."""
    q = query.strip()
    if not q:
        return None
    params = urllib.parse.urlencode({"query": q})
    url = f"{TMDB_API_BASE}/search/movie?{params}"
    data = _request_json(url)
    return _best_with_poster_from_results(q, data.get("results") or [])


def search_movie_best(query: str) -> dict | None:
    q = query.strip()
    if not q:
        return None
    params = urllib.parse.urlencode({"query": q})
    url = f"{TMDB_API_BASE}/search/movie?{params}"
    data = _request_json(url)
    return _best_from_results(q, data.get("results") or [])


def search_tv_best(query: str) -> dict | None:
    q = query.strip()
    if not q:
        return None
    params = urllib.parse.urlencode({"query": q})
    url = f"{TMDB_API_BASE}/search/tv?{params}"
    data = _request_json(url)
    return _best_from_results(q, data.get("results") or [])


def search_tv_best_with_poster(query: str) -> dict | None:
    """Return one TMDb TV result (has poster_path) or None."""
    q = query.strip()
    if not q:
        return None
    params = urllib.parse.urlencode({"query": q})
    url = f"{TMDB_API_BASE}/search/tv?{params}"
    data = _request_json(url)
    return _best_with_poster_from_results(q, data.get("results") or [])


def search_best_media_with_poster(query: str, *, prefer: Prefer = "auto") -> tuple[dict | None, MediaKind | None]:
    """
    Pick one movie or TV hit with a poster.
    ``auto`` chooses whichever has higher TMDb ``popularity`` (ties → movie).
    """
    q = query.strip()
    if not q:
        return None, None
    if prefer == "movie":
        m = search_movie_best_with_poster(q)
        return (m, "movie") if m else (None, None)
    if prefer == "tv":
        t = search_tv_best_with_poster(q)
        return (t, "tv") if t else (None, None)

    m = search_movie_best_with_poster(q)
    t = search_tv_best_with_poster(q)
    if m and not t:
        return m, "movie"
    if t and not m:
        return t, "tv"
    if not m and not t:
        return None, None
    pm = float(m.get("popularity") or 0.0)
    pt = float(t.get("popularity") or 0.0)
    if pt > pm:
        return t, "tv"
    return m, "movie"


def search_best_media(query: str, *, prefer: Prefer = "auto") -> tuple[dict | None, MediaKind | None]:
    """Pick one movie or TV hit by popularity (poster not required)."""
    q = query.strip()
    if not q:
        return None, None
    if prefer == "movie":
        m = search_movie_best(q)
        return (m, "movie") if m else (None, None)
    if prefer == "tv":
        t = search_tv_best(q)
        return (t, "tv") if t else (None, None)

    m = search_movie_best(q)
    t = search_tv_best(q)
    if m and not t:
        return m, "movie"
    if t and not m:
        return t, "tv"
    if not m and not t:
        return None, None
    pm = float(m.get("popularity") or 0.0)
    pt = float(t.get("popularity") or 0.0)
    if pt > pm:
        return t, "tv"
    return m, "movie"


def fetch_media_images(kind: MediaKind, media_id: int) -> dict:
    if kind == "movie":
        url = f"{TMDB_API_BASE}/movie/{int(media_id)}/images"
    else:
        url = f"{TMDB_API_BASE}/tv/{int(media_id)}/images"
    return _request_json(url)


def _logo_path_from_images(images: dict) -> str | None:
    logos = images.get("logos") or []
    if not logos:
        return None

    # Always prefer English logos. If none exist, treat as no logo (do not fall back).
    en = [l for l in logos if str((l or {}).get("iso_639_1") or "").lower() == "en"]
    if not en:
        return None
    best = max(en, key=lambda l: float((l or {}).get("vote_average") or 0.0))
    return (best or {}).get("file_path")  # type: ignore[return-value]


def _backdrop_is_no_language(item: dict) -> bool:
    """TMDb uses ``iso_639_1: null`` (and sometimes missing/empty) for language-neutral backdrops."""
    iso = item.get("iso_639_1")
    if iso is None:
        return True
    if isinstance(iso, str) and iso.strip() == "":
        return True
    return False


def _random_backdrop_path(images: dict) -> str | None:
    backs = images.get("backdrops") or []
    neutral = [
        b
        for b in backs
        if isinstance(b, dict) and _backdrop_is_no_language(b) and b.get("file_path")
    ]
    if not neutral:
        return None
    choice = random.choice(neutral)
    return choice.get("file_path")


def _maybe_delete_pulled(path: Path) -> None:
    if pulled_path_is_under_pulled_dir(path) and auto_delete_pulled_media():
        try:
            path.unlink()
        except OSError:
            pass


def _display_title(item: dict, kind: MediaKind) -> str:
    if kind == "movie":
        return str(item.get("title") or item.get("original_title") or "movie")
    return str(item.get("name") or item.get("original_name") or "TV")


def download_poster_to_pulled(item: dict, kind: MediaKind) -> tuple[bool, str, Path | None]:
    """
    Save poster under pigeonPulledMedia as ``tmdb_m_<id>`` or ``tmdb_tv_<id>`` (IDs differ by media type).
    """
    mid = item.get("id")
    ppath = item.get("poster_path")
    title = _display_title(item, kind)
    if mid is None or not ppath:
        return False, "TMDb result missing id or poster_path.", None
    ext = Path(str(ppath)).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    tag = "m" if kind == "movie" else "tv"
    pulled = pigeon_pulled_media_dir()
    dest = pulled / f"tmdb_{tag}_{int(mid)}{ext}"
    image_url = f"{IMG_BASE}{ppath}"
    try:
        _download_binary(image_url, dest)
    except urllib.error.HTTPError as e:
        return False, f"Poster download failed ({e.code}): {e.reason}", None
    except urllib.error.URLError as e:
        return False, f"Poster download failed: {e.reason}", None
    except OSError as e:
        return False, f"Could not save poster: {e}", None
    kind_label = "movie" if kind == "movie" else "TV"
    return True, f"{title} ({kind_label}) → {dest.name}", dest


def download_logo_to_pulled(item: dict, kind: MediaKind, file_path: str) -> tuple[bool, str, Path | None]:
    mid = item.get("id")
    if mid is None or not file_path:
        return False, "TMDb logo path missing.", None
    ext = Path(str(file_path)).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".svg"):
        ext = ".png"
    tag = "m" if kind == "movie" else "tv"
    dest = pigeon_pulled_media_dir() / f"tmdb_logo_{tag}_{int(mid)}{ext}"
    image_url = f"{IMG_LOGO_BASE}{file_path}"
    try:
        _download_binary(image_url, dest)
    except urllib.error.HTTPError as e:
        return False, f"Logo download failed ({e.code}): {e.reason}", None
    except urllib.error.URLError as e:
        return False, f"Logo download failed: {e.reason}", None
    except OSError as e:
        return False, f"Could not save logo: {e}", None
    return True, dest.name, dest


def download_backdrop_to_pulled(item: dict, kind: MediaKind, file_path: str) -> tuple[bool, str, Path | None]:
    mid = item.get("id")
    if mid is None or not file_path:
        return False, "TMDb backdrop path missing.", None
    ext = Path(str(file_path)).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    tag = "m" if kind == "movie" else "tv"
    rand = secrets.token_hex(4)
    dest = pigeon_pulled_media_dir() / f"tmdb_bd_{tag}_{int(mid)}_{rand}{ext}"
    image_url = f"{IMG_BACKDROP_BASE}{file_path}"
    try:
        _download_binary(image_url, dest)
    except urllib.error.HTTPError as e:
        return False, f"Backdrop download failed ({e.code}): {e.reason}", None
    except urllib.error.URLError as e:
        return False, f"Backdrop download failed: {e.reason}", None
    except OSError as e:
        return False, f"Could not save backdrop: {e}", None
    return True, dest.name, dest


def fetch_tmdb_poster_to_pulled(query: str, *, prefer: Prefer = "auto") -> tuple[bool, str, Path | None]:
    """Search TMDb (movie and/or TV) and download best-match poster to pigeonPulledMedia."""
    try:
        item, kind = search_best_media_with_poster(query, prefer=prefer)
    except RuntimeError as e:
        return False, str(e), None
    except urllib.error.HTTPError as e:
        return False, f"TMDb API error ({e.code}): {e.reason}", None
    except urllib.error.URLError as e:
        return False, f"TMDb network error: {e.reason}", None
    except (json.JSONDecodeError, OSError, ValueError) as e:
        return False, str(e), None
    if item is None or kind is None:
        return False, "No movie or TV show found with a poster for that search.", None
    return download_poster_to_pulled(item, kind)


def apply_tmdb_movie_query(query: str, *, prefer: Prefer = "auto") -> tuple[bool, str, np.ndarray | None]:
    """
    Search TMDb, prefer cached logo when present; pull missing assets and cache as
    ``{Title}_{Logo|Backdrop}`` in pigeonReFormattedMedia.

    Always picks a **random** backdrop from TMDb image results (not served from cache).

    Returns ``(ok, message, backdrop_master_bgr_or_none)`` where master is BGR scaled to 2160px tall
    for the scene compositor, or None if no backdrop could be loaded.
    """
    q = query.strip()
    if not q:
        return False, "Empty search.", None

    try:
        item, kind = search_best_media(q, prefer=prefer)
    except RuntimeError as e:
        return False, str(e), None
    except urllib.error.HTTPError as e:
        return False, f"TMDb API error ({e.code}): {e.reason}", None
    except urllib.error.URLError as e:
        return False, f"TMDb network error: {e.reason}", None
    except (json.JSONDecodeError, OSError, ValueError) as e:
        return False, str(e), None

    if item is None or kind is None:
        return False, "No movie or TV show found for that search.", None

    display_title = _display_title(item, kind)
    tk = title_key(display_title)
    parts: list[str] = [display_title]

    # --- Images bundle (logo + random backdrop) ---
    backdrop_master: np.ndarray | None = None
    try:
        images = fetch_media_images(kind, int(item["id"]))
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        images = {}

    # --- Logo (English-only; cache first) ---
    logo_cached = find_cached_reformatted_asset(tk, ASSET_LOGO_EN)
    if logo_cached is not None:
        parts.append("logo: en cache")
    else:
        lp = _logo_path_from_images(images)
        if lp:
            ok_l, _msg_l, logo_path = download_logo_to_pulled(item, kind, lp)
            if ok_l and logo_path is not None:
                try:
                    copy_pulled_to_reformatted(logo_path, tk, ASSET_LOGO_EN)
                    parts.append(f"logo: {logo_path.name}")
                except OSError as e:
                    parts.append(f"logo: copy failed ({e})")
                _maybe_delete_pulled(logo_path)
            else:
                parts.append("logo: skip")
        else:
            parts.append("logo: none")

    # --- Backdrop: always random from API ---
    bp = _random_backdrop_path(images)
    if not bp:
        parts.append("backdrop: none")
    else:
        ok_b, _msg_b, bd_pulled = download_backdrop_to_pulled(item, kind, bp)
        if ok_b and bd_pulled is not None:
            try:
                copy_pulled_to_reformatted(bd_pulled, tk, ASSET_BACKDROP)
            except OSError as e:
                parts.append(f"backdrop: reformatted copy failed ({e})")
            else:
                parts.append(f"backdrop: {bd_pulled.name}")
            backdrop_master = backdrop_master_bgr_from_file(bd_pulled)
            _maybe_delete_pulled(bd_pulled)
        else:
            parts.append("backdrop: download failed")

    summary = " | ".join(parts)
    # Prefix title_key + display_title so the UI can render a text fallback when no English logo exists.
    return True, f"{tk}::{display_title}::{summary}", backdrop_master
