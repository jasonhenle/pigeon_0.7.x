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
import re
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


def _title_norm_matches_exact_tv_series_filter(must_norm: str, title_norm: str) -> bool:
    """
    For short-show canonical queries: keep TMDb rows whose name equals the series **or** adds only
    numeric tokens after the series (e.g. ``Saturday Night Live (1975)`` → ``saturday night live 1975``).

    Rejects spin-offs with word suffixes (``Saturday Night Live: Christmas`` → ``… christmas``).
    """
    if not must_norm or not title_norm:
        return False
    if title_norm == must_norm:
        return True
    prefix = must_norm + " "
    if not title_norm.startswith(prefix):
        return False
    rest = title_norm[len(prefix) :].strip()
    if not rest:
        return False
    for tok in rest.split():
        if not tok.isdigit():
            return False
    return True


# App / channel branding — not a movie or episode title (TMDb search yields wrong hits).
_DEGENERATE_TMDB_QUERIES = frozenset(
    {
        "disney",
        "disney+",
        "disney plus",
        "disney+ 365",
        "netflix",
        "hulu",
        "max",
        "peacock",
        "apple tv",
        "youtube",
        "paramount+",
        "paramount plus",
        "prime video",
        "amazon video",
        "roku",
        "home",
        "settings",
        "hbo max",
        "hbomax",
    }
)


def is_degenerate_tmdb_query(q: str) -> bool:
    """
    True if ``q`` should not be sent to TMDb alone (streaming app name, splash branding, etc.).
    """
    raw = (q or "").strip()
    if not raw or len(raw) < 2:
        return True
    n = _norm_query(raw)
    if not n:
        return True
    if n in _DEGENERATE_TMDB_QUERIES:
        return True
    # "disney+ originals", "disney+ 365", etc.
    if "disney" in n and ("365" in n or n.endswith(" original") or n.endswith(" originals")):
        return True
    if n.replace(" ", "").isdigit():
        return True
    return False


# Keys match ``_norm_query()`` form (e.g. ``snl`` for ``SNL``, full title for specials).
_SHORT_SHOW_CANONICAL_QUERIES: dict[str, str] = {
    "snl": "Saturday Night Live",
    "saturday night live": "Saturday Night Live",
}

# When TMDb search + ``prefer`` heuristics would still pick the wrong kind (e.g. SNL holiday
# compilations are **movies**, while the main show is TV 1667), resolve by id.
_SHORT_SHOW_TMDB_TV_ID_BY_NORM: dict[str, int] = {
    "saturday night live": 1667,
}

# When TMDb lists compilation specials as **movies** (e.g. ``Saturday Night Live: Christmas``), inferred
# ``prefer=movie`` from pyatv ``Video`` must still resolve the **series** for artwork. Keys = ``_norm_query``
# form of the canonical display title (same namespace as ``_exact_tv_title_norm_for_known_series_query``).
_SHORT_SHOW_TMDB_TV_ID_BY_NORM: dict[str, int] = {
    "saturday night live": 1667,
}


def _compact_norm_for_acronym(s: str) -> str:
    return "".join(ch for ch in _norm_query(s) if ch.isalnum())


def _canonical_series_from_dash_pair(left: str, right: str) -> str | None:
    """
    Peacock / tvOS sometimes send ``SNL - Sketch`` or ``Sketch - SNL``.
    If either side is a known acronym, return the canonical series search string.
    """
    le, ri = left.strip(), right.strip()
    if not le or not ri:
        return None
    lk = _compact_norm_for_acronym(le)
    rk = _compact_norm_for_acronym(ri)
    if rk in _SHORT_SHOW_CANONICAL_QUERIES:
        return _SHORT_SHOW_CANONICAL_QUERIES[rk]
    if lk in _SHORT_SHOW_CANONICAL_QUERIES:
        return _SHORT_SHOW_CANONICAL_QUERIES[lk]
    return None


def canonical_tv_title_if_sketch_show_compound(display_title: str) -> str | None:
    """
    TMDb sometimes returns a TV row whose ``name`` is ``Sketch - SNL``, ``SNL - Sketch``,
    or ``Saturday Night Live: Christmas``-style episode/special labels.
    Replace with the canonical series title when a known show appears on the left (dash or colon).
    """
    q0 = _normalize_title_for_show_split(display_title or "")
    if not q0:
        return None
    pair = _sketch_show_dash_pair(q0)
    if pair is not None:
        c = _canonical_series_from_dash_pair(pair[0], pair[1])
        if c:
            return c
    for sep in (":", "\uff1a"):
        if sep in q0:
            a, b = q0.split(sep, 1)
            a_s, b_s = a.strip(), b.strip()
            if not a_s or not b_s:
                continue
            c = _canonical_series_from_dash_pair(a_s, b_s)
            if c:
                return c
            full = _SHORT_SHOW_CANONICAL_QUERIES.get(_norm_query(a_s))
            if full:
                return full
    return None


_UNICODE_DASH_CHARS = frozenset(
    "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"  # hyphen, dashes, minus (not ASCII -)
)

# NBSP and other spaces that break naive ``" - "`` substring checks (Peacock / tvOS metadata).
_SPACE_LIKE_RE = re.compile(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
# ``Show - sketch`` with flexible space and any common dash (ASCII or unicode).
_SHOW_EPISODE_SEP_RE = re.compile(
    r"\s+[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d]\s+"
)
# Metadata sometimes omits spaces around the hyphen (``Papryus-SNL``, ``Papryus -SNL``).
_LOOSE_SHOW_EPISODE_SEP_RE = re.compile(
    r"\s*[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d]\s*"
)


def _sketch_show_dash_pair(q0: str) -> tuple[str, str] | None:
    """Return ``(left, right)`` for ``Sketch - Show`` / tight-hyphen variants, or None."""
    m = _SHOW_EPISODE_SEP_RE.split(q0, maxsplit=1)
    if len(m) == 2:
        a, b = m[0].strip(), m[1].strip()
        if a and b:
            return a, b
    parts = _LOOSE_SHOW_EPISODE_SEP_RE.split(q0, maxsplit=1)
    if len(parts) == 2:
        a, b = parts[0].strip(), parts[1].strip()
        if a and b:
            return a, b
    return None


def _normalize_unicode_dashes_for_episode_titles(s: str) -> str:
    """Map unicode dashes to `` - `` so ``SNL–Sketch`` (en dash) splits like ``SNL - Sketch``."""
    if not s:
        return s
    parts: list[str] = []
    for ch in s:
        if ch in _UNICODE_DASH_CHARS:
            parts.append(" - ")
        else:
            parts.append(ch)
    t = "".join(parts)
    while "   " in t:
        t = t.replace("   ", " ")
    while "  " in t:
        t = t.replace("  ", " ")
    return t.strip()


def _normalize_title_for_show_split(s: str) -> str:
    """Unicode dashes → spaced hyphen; NBSP-like → space; collapse runs (for reliable ``Show - x`` splits)."""
    if not s:
        return s
    t = _normalize_unicode_dashes_for_episode_titles(s.strip())
    t = _SPACE_LIKE_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def colon_prefix_show_query(raw: str) -> str | None:
    """
    If metadata looks like ``Show: segment`` or ``Show - sketch`` (guest, sketch, episode label),
    return the show side for TMDb when the full string would match the wrong thing or miss the series.
    """
    q0 = _normalize_title_for_show_split(raw or "")
    if not q0:
        return None

    def _split_show(sep: str) -> str | None:
        if sep not in q0:
            return None
        left, right = q0.split(sep, 1)
        left, right = left.strip(), right.strip()
        if not left or not right:
            return None
        if len(left) < 2:
            return None
        if is_degenerate_tmdb_query(left):
            return None
        if left.lower() == q0.lower():
            return None
        return left

    parts = _SHOW_EPISODE_SEP_RE.split(q0, maxsplit=1)
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()
        if left and right:
            canon = _canonical_series_from_dash_pair(left, right)
            if canon:
                return canon
            if (
                len(left) >= 2
                and not is_degenerate_tmdb_query(left)
                and left.lower() != q0.lower()
            ):
                return left

    for sep in (" - ", " – "):
        got = _split_show(sep)
        if got:
            return got
    for sep in ("\u2014", "\u2013"):
        got = _split_show(sep)
        if got:
            return got
    for sep in (":", "\uff1a"):
        got = _split_show(sep)
        if got:
            return got
    return None


def refine_tmdb_search_query(raw: str | None) -> str | None:
    """
    Last-mile cleanup for any metadata source: unicode dashes, then ``Show - segment`` / colon stripping.
    Safe to call on strings that already went through pyatv heuristics (idempotent for plain titles).
    """
    if raw is None:
        return None
    s = _normalize_title_for_show_split(str(raw).strip())
    if not s:
        return None
    pick = colon_prefix_show_query(s)
    out = (pick or s).strip()
    return out or None


def canonical_tv_display_name_for_search_query(search_query: str) -> str | None:
    """
    When the user/device search resolves to a known acronym (e.g. ``SNL``), use TMDb’s full series
    name for on-screen title/logo cache even if TMDb matched a sketch row.
    """
    p = (search_query or "").strip()
    m = re.match(r"(?is)^tv\s+(.+)$", p)
    if m:
        p = m.group(1).strip()
    key = _norm_query(p)
    return _SHORT_SHOW_CANONICAL_QUERIES.get(key)


def _exact_tv_title_norm_for_known_series_query(q: str) -> str | None:
    """
    If ``q`` maps to a canonical series in ``_SHORT_SHOW_CANONICAL_QUERIES`` (via
    :func:`canonical_tv_display_name_for_search_query`, including after
    :func:`refine_tmdb_search_query`), return that series’ normalized title.

    TV search keeps only rows whose title matches the canonical series (normalized), optionally with
    **numeric-only** trailing tokens (e.g. TMDb’s ``(1975)`` in the title). When this returns
    non-``None``, media pick uses **TV** for that query (and a fixed TMDb id when configured) even if
    ``prefer`` is ``movie``, so compilation **movies** with the same words cannot beat the series.
    """
    p = (q or "").strip()
    m = re.match(r"(?is)^tv\s+(.+)$", p)
    if m:
        p = m.group(1).strip()
    canon = canonical_tv_display_name_for_search_query(p)
    if canon:
        return _norm_query(canon)
    r = refine_tmdb_search_query(p) or p
    if r.strip() != p.strip():
        canon = canonical_tv_display_name_for_search_query(r)
        if canon:
            return _norm_query(canon)
    return None


def resolve_tmdb_query_from_now_playing_fields(
    *,
    base_query: str | None,
    title: object | None = None,
    series_name: object | None = None,
    artist: object | None = None,
    album: object | None = None,
    episode_title: object | None = None,
) -> str | None:
    """
    Build the TMDb search string from pyatv-style fields plus the heuristic ``base_query``.

    Prefer the same canonical series title used in the UI (e.g. ``Saturday Night Live``) when any
    field is a sketch–show compound (``… - SNL``) or a mapped short name (``SNL`` alone).
    """

    def _field(x: object | None) -> str | None:
        if x is None:
            return None
        t = str(x).strip()
        return t or None

    ordered: list[str] = []
    for x in (series_name, title, episode_title, base_query, artist, album):
        s = _field(x)
        if s and s not in ordered:
            ordered.append(s)

    for s in ordered:
        compound = canonical_tv_title_if_sketch_show_compound(s)
        if compound:
            return compound
    for s in ordered:
        r = refine_tmdb_search_query(s) or s
        canon = canonical_tv_display_name_for_search_query(r)
        if canon:
            return canon

    if base_query is None:
        return None
    out = refine_tmdb_search_query(str(base_query).strip()) or str(base_query).strip()
    if not out:
        return None
    compound = canonical_tv_title_if_sketch_show_compound(out)
    if compound:
        return compound
    canon = canonical_tv_display_name_for_search_query(out)
    return canon or out


def equivalent_tmdb_search_queries(a: str, b: str) -> bool:
    """
    True when two query strings mean the same show for alternation / dedupe
    (e.g. primary ``SNL`` vs metadata title ``Papyrus - SNL``).
    """
    ra = refine_tmdb_search_query((a or "").strip()) or (a or "").strip()
    rb = refine_tmdb_search_query((b or "").strip()) or (b or "").strip()
    if ra.lower() == rb.lower():
        return True
    ca = canonical_tv_display_name_for_search_query(ra)
    cb = canonical_tv_display_name_for_search_query(rb)
    if ca and cb and ca.lower() == cb.lower():
        return True
    if ca and rb.lower() == ca.lower():
        return True
    if cb and ra.lower() == cb.lower():
        return True
    return False


def _tmdb_query_variants(raw: str) -> list[str]:
    """
    Extra query strings to try when now-playing metadata appends app names or episode titles
    (common from Apple TV), which TMDb will not match as a single search phrase.

    **Show–segment** prefixes (``Show - sketch``, colon, etc.) are tried **before** the full string
    so TMDb does not latch onto a sketch/special that matches the compound title.
    """
    q0 = _normalize_title_for_show_split((raw or "").strip())
    if not q0:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        t = s.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    prefixes: list[str] = []

    def push_left(left: str, right: str) -> None:
        le = left.strip()
        ri = right.strip()
        if not le or not ri or len(le) < 2:
            return
        if is_degenerate_tmdb_query(le):
            return
        if le.lower() == q0.lower():
            return
        prefixes.append(le)

    rx_parts = _SHOW_EPISODE_SEP_RE.split(q0, maxsplit=1)
    if len(rx_parts) == 2:
        lx, rx = rx_parts[0].strip(), rx_parts[1].strip()
        cnp = _canonical_series_from_dash_pair(lx, rx)
        if cnp:
            add(cnp)
        else:
            push_left(lx, rx)

    if "|" in q0:
        a, b = q0.split("|", 1)
        push_left(a, b)
    if " - " in q0:
        a, b = q0.split(" - ", 1)
        push_left(a, b)
    for sep in (" – ", "\u2014", "\u2013"):
        if sep in q0:
            a, b = q0.split(sep, 1)
            push_left(a, b)
    for sep in (":", "\uff1a"):
        if sep in q0:
            a, b = q0.split(sep, 1)
            push_left(a, b)

    uniq_prefix: list[str] = []
    for p in prefixes:
        if p not in uniq_prefix:
            uniq_prefix.append(p)
    # Shortest first: e.g. ``SNL`` before a longer accidental prefix.
    uniq_prefix.sort(key=len)
    for p in uniq_prefix:
        add(p)
        key = _norm_query(p)
        canon = _SHORT_SHOW_CANONICAL_QUERIES.get(key)
        if canon:
            add(canon)

    add(q0)
    if "|" in q0:
        add(q0.split("|", 1)[0])
    for sep in ("\u2014", "\u2013", " – "):
        if sep in q0:
            add(q0.split(sep, 1)[0])
    for sep in (":", "\uff1a"):
        if sep in q0:
            left = q0.split(sep, 1)[0].strip()
            if left and not is_degenerate_tmdb_query(left):
                add(left)
    return out


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

    TV queries that map to ``_SHORT_SHOW_CANONICAL_QUERIES`` are pre-filtered so every candidate’s
    title **equals** the canonical series name (normalized), or that name plus **numeric-only** suffix
    tokens (TMDb’s ``(1975)`` style); those hits are not chosen on loose substring strength alone.

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


def _best_with_poster_from_results(
    query: str,
    results: list,
    *,
    tv_title_must_equal_norm: str | None = None,
) -> dict | None:
    if not isinstance(results, list) or not results:
        return None
    with_poster = [r for r in results if isinstance(r, dict) and r.get("poster_path")]
    if not with_poster:
        return None
    if tv_title_must_equal_norm:
        with_poster = [
            r
            for r in with_poster
            if any(
                _title_norm_matches_exact_tv_series_filter(tv_title_must_equal_norm, _norm_query(t))
                for t in _result_titles(r)
            )
        ]
        if not with_poster:
            return None
    # Prefer exact / whole-word title matches from the real search query; tie-break by
    # shorter title, then TMDb popularity. Fall back to full pool only if every rank is 0.
    scored = [(r, _match_rank(query, r)) for r in with_poster]
    best_key = max(rank for _, rank in scored)
    pool = [r for r, rank in scored if rank == best_key] if best_key[0] > 0 else with_poster
    return max(pool, key=lambda r: float(r.get("popularity") or 0.0))


def _best_from_results(
    query: str,
    results: list,
    *,
    tv_title_must_equal_norm: str | None = None,
) -> dict | None:
    """Highest popularity among dict results (no poster requirement)."""
    if not isinstance(results, list) or not results:
        return None
    items = [r for r in results if isinstance(r, dict)]
    if not items:
        return None
    if tv_title_must_equal_norm:
        items = [
            r
            for r in items
            if any(
                _title_norm_matches_exact_tv_series_filter(tv_title_must_equal_norm, _norm_query(t))
                for t in _result_titles(r)
            )
        ]
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
    en = _exact_tv_title_norm_for_known_series_query(q)
    return _best_from_results(q, data.get("results") or [], tv_title_must_equal_norm=en)


def search_tv_best_with_poster(query: str) -> dict | None:
    """Return one TMDb TV result (has poster_path) or None."""
    q = query.strip()
    if not q:
        return None
    params = urllib.parse.urlencode({"query": q})
    url = f"{TMDB_API_BASE}/search/tv?{params}"
    data = _request_json(url)
    en = _exact_tv_title_norm_for_known_series_query(q)
    return _best_with_poster_from_results(
        q, data.get("results") or [], tv_title_must_equal_norm=en
    )


def _tmdb_tv_detail(tv_id: int) -> dict | None:
    try:
        data = _request_json(f"{TMDB_API_BASE}/tv/{int(tv_id)}")
    except (RuntimeError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("id") is None:
        return None
    return data


def _forced_tmdb_tv_item_for_canonical_query(q: str, *, require_poster: bool) -> dict | None:
    """
    For queries that map to :data:`_SHORT_SHOW_CANONICAL_QUERIES`, return the fixed TMDb TV row from
    ``/tv/{id}`` when listed in :data:`_SHORT_SHOW_TMDB_TV_ID_BY_NORM`.

    Holiday / clip compilations for the same franchise are often **movies** on TMDb; Apple TV often
    reports them as ``Video`` so ``prefer`` becomes ``movie`` and would otherwise beat the series.
    """
    en = _exact_tv_title_norm_for_known_series_query(q)
    if en is None:
        return None
    tv_id = _SHORT_SHOW_TMDB_TV_ID_BY_NORM.get(en)
    if tv_id is None:
        return None
    detail = _tmdb_tv_detail(tv_id)
    if detail is None:
        return None
    if require_poster and not detail.get("poster_path"):
        return None
    return detail


def _search_best_media_with_poster_one(q: str, *, prefer: Prefer) -> tuple[dict | None, MediaKind | None]:
    if not q:
        return None, None
    en = _exact_tv_title_norm_for_known_series_query(q)
    if en is not None:
        hit = _forced_tmdb_tv_item_for_canonical_query(q, require_poster=True)
        if hit is None:
            hit = search_tv_best_with_poster(q)
        if hit is not None:
            return hit, "tv"
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


def search_best_media_with_poster(query: str, *, prefer: Prefer = "auto") -> tuple[dict | None, MediaKind | None]:
    """
    Pick one movie or TV hit with a poster.
    ``auto`` chooses whichever has higher TMDb ``popularity`` (ties → movie).
    """
    for q in _tmdb_query_variants(query):
        hit = _search_best_media_with_poster_one(q, prefer=prefer)
        if hit[0] is not None:
            return hit
    return None, None


def _search_best_media_one(q: str, *, prefer: Prefer) -> tuple[dict | None, MediaKind | None]:
    if not q:
        return None, None
    en = _exact_tv_title_norm_for_known_series_query(q)
    if en is not None:
        hit = _forced_tmdb_tv_item_for_canonical_query(q, require_poster=False)
        if hit is None:
            hit = search_tv_best(q)
        if hit is not None:
            return hit, "tv"
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


def search_best_media(query: str, *, prefer: Prefer = "auto") -> tuple[dict | None, MediaKind | None]:
    """Pick one movie or TV hit by popularity (poster not required)."""
    for q in _tmdb_query_variants(query):
        hit = _search_best_media_one(q, prefer=prefer)
        if hit[0] is not None:
            return hit
    return None, None


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
        q0 = query.strip()
        variants = _tmdb_query_variants(q0)
        tried_line = (
            "Variants tried: " + ", ".join(repr(x) for x in variants) + "\n"
            if len(variants) > 1
            else ""
        )
        return (
            False,
            "No movie or TV show found with a poster for that search.\n\n"
            f"Searched: {q0!r}\n{tried_line}\n"
            "Tips: In the command bar use tv Your Show or movie Your Film; use the series or "
            "film title only. If the string included an app, episode name, or a colon "
            "(Show: guest), Pigeon already tried shortened variants.",
            None,
        )
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
    q = refine_tmdb_search_query(q) or q

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
        variants = _tmdb_query_variants(q)
        tried_line = (
            "Variants tried: " + ", ".join(repr(x) for x in variants) + "\n"
            if len(variants) > 1
            else ""
        )
        return (
            False,
            "No movie or TV show found for that search.\n\n"
            f"Searched: {q!r}\n{tried_line}\n"
            "Tips: Use tv Your Show or movie Your Film in the command bar; try the main title "
            "only. Apple TV sometimes sends a label TMDb does not recognize (episode titles, apps, "
            "Show: guest lines, or extras). Check spelling and network — API errors show a different message.",
            None,
        )

    display_title = _display_title(item, kind)
    # TMDb may classify an SNL sketch row as a **movie**; still normalize the on-screen title.
    swap = canonical_tv_title_if_sketch_show_compound(display_title)
    if swap:
        display_title = swap
    if kind == "tv":
        canon = canonical_tv_display_name_for_search_query(q)
        if canon:
            display_title = canon
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
