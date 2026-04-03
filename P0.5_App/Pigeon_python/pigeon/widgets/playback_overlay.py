"""Playback chrome: streaming badge, receiver-driven audio lines, subtle ``pigeon`` wordmark (grid-aligned)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr, scale_bgra_rgb
from pigeon.design import rect_for_span_at_cell
from pigeon.font_paths import resolve_ui_font_bold, resolve_ui_font_extrabold
from pigeon.image_ui_protocol import load_image_bgra
from pigeon.widgets.status_bar import DesignPatch

# Service badge + audio lines (audioConfig): 10% smaller, centered in grid cells.
AUDIO_CONFIG_SCALE = 0.9

PATCH_LAYER_WORDMARK = "wordmark"
PATCH_LAYER_STREAMING_BADGE = "streaming_badge"
PATCH_LAYER_RECEIVER_AUDIO = "receiver_audio"
# Idle UI: streaming badge only (receiver lines stay full brightness).
IDLE_DIM_STREAMING_BADGE_RGB_SCALE = 0.1


def _receiver_audio_display_line(raw: object) -> str:
    """Strip placeholder / empty receiver strings so the overlay can omit those rows."""
    s = str(raw or "").strip()
    if not s or s.lower() == "no audio":
        return ""
    return s


# Volume unknown / idle placeholders from receiver poll — do not draw (glyphs read as a slab).
_VOLUME_PLACEHOLDER_CHARS = frozenset("—–-−")  # em dash, en dash, ASCII hyphen, minus sign


def _receiver_volume_display_line(raw: object) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.lower() == "no audio":
        return ""
    if all(c in _VOLUME_PLACEHOLDER_CHARS or c.isspace() for c in s):
        return ""
    if s.lower() in ("n/a", "na", "none", "--"):
        return ""
    return s

# Large wordmark: top-left cell [2,3], bottom-right [5,15] → 13×4 cells.
_PIGEON_WORDMARK = "pigeon"
_PIGEON_WORDMARK_ROW = 2
_PIGEON_WORDMARK_COL = 3
_PIGEON_WORDMARK_SPAN_W = 13
_PIGEON_WORDMARK_SPAN_H = 4
# 90% black → ~10% white (RGB).
_PIGEON_WORDMARK_RGBA = (26, 26, 26, 255)


def _pick_extrabold_font_path() -> str:
    p = resolve_ui_font_extrabold()
    if p:
        return p
    p = resolve_ui_font_bold()
    if p:
        return p
    return "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def _fit_font_to_box(text: str, max_w: int, max_h: int) -> ImageFont.ImageFont:
    path = _pick_extrabold_font_path()
    lo, hi = 6, max(max_h * 4, max_w * 2, 400)
    best = ImageFont.truetype(path, 6) if path else ImageFont.load_default()
    probe = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            f = ImageFont.truetype(path, mid)
        except OSError:
            f = ImageFont.load_default()
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        tw, th = r - l, b - t
        if tw <= max_w and th <= max_h:
            best = f
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _pigeon_wordmark_patch_bgra(w: int, h: int) -> np.ndarray:
    if w < 2 or h < 2:
        return np.zeros((h, w, 4), dtype=np.uint8)
    pad = max(4, int(round(min(w, h) * 0.03)))
    mw, mh = max(4, w - 2 * pad), max(4, h - 2 * pad)
    font = _fit_font_to_box(_PIGEON_WORDMARK, mw, mh)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    l, t, r, b = draw.textbbox((0, 0), _PIGEON_WORDMARK, font=font)
    tw, th = r - l, b - t
    # Centered in the full [2,3]–[5,15] span.
    x = (w - tw) // 2 - l
    y = (h - th) // 2 - t
    draw.text((x, y), _PIGEON_WORDMARK, font=font, fill=_PIGEON_WORDMARK_RGBA)
    rgba = np.asarray(img)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def _text_patch_bgra(
    text: str,
    w: int,
    h: int,
    *,
    align: str = "left",
    fill_rgba: tuple[int, int, int, int] = (255, 255, 255, 255),
    fit_max_h: int | None = None,
) -> np.ndarray:
    if w < 2 or h < 2 or not text:
        return np.zeros((h, w, 4), dtype=np.uint8)
    pad = max(2, int(round(min(w, h) * 0.06)))
    mw, mh = max(4, w - 2 * pad), max(4, h - 2 * pad)
    if fit_max_h is not None:
        mh = min(mh, max(4, fit_max_h))
    font = _fit_font_to_box(text, mw, mh)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    if align == "right":
        x = w - pad - tw - l
    elif align == "center":
        x = (w - tw) // 2 - l
    else:
        x = pad - l
    y = (h // 2) - (th // 2) - t
    draw.text((x, y), text, font=font, fill=fill_rgba)
    rgba = np.asarray(img)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)


def _image_contain_center_bgra(
    src_bgra: np.ndarray, w: int, h: int
) -> np.ndarray:
    if src_bgra is None or src_bgra.size == 0 or w < 1 or h < 1:
        return np.zeros((h, w, 4), dtype=np.uint8)
    sh, sw = src_bgra.shape[:2]
    if sh < 1 or sw < 1:
        return np.zeros((h, w, 4), dtype=np.uint8)
    scale = min(w / float(sw), h / float(sh))
    tw = max(1, int(round(sw * scale)))
    th = max(1, int(round(sh * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(src_bgra, (tw, th), interpolation=interp)
    out = np.zeros((h, w, 4), dtype=np.uint8)
    x0 = max(0, (w - tw) // 2)
    y0 = max(0, (h - th) // 2)
    out[y0 : y0 + th, x0 : x0 + tw] = resized
    return out


@dataclass
class AudioConfig:
    """
    Optional ``pigeon`` wordmark in grid [2,3]–[5,15] (hidden when TMDb backdrop artwork is showing);
    row 8: optional streaming-service badge; up to three audio lines from ``receiver_state``
    (incoming, surround/config, volume); placeholder/unknown values are omitted.
    """

    assets_dir: Path
    pigeon_logo_path: Path | None = None  # unused; PNG wordmark removed
    receiver_state: dict[str, str] = field(
        default_factory=lambda: {"incoming": "", "config": "", "volume": ""}
    )
    service_badge: dict[str, object] = field(
        default_factory=lambda: {"show": False, "filename": "", "label": ""}
    )
    overlay_flags: dict[str, bool] = field(
        default_factory=lambda: {"hide_wordmark_for_artwork": False}
    )

    badge_row: int = 8
    badge_col: int = 5
    badge_span: tuple[int, int] = (2, 1)
    audio_row: int = 8
    audio_col: int = 7
    audio_span_wide: int = 4

    _cached: list[DesignPatch] | None = field(default=None, repr=False)
    _receiver_sig: tuple[str, str, str, bool, str, str, bool] | None = field(
        default=None, init=False, repr=False
    )
    _warned_badge_files: set[str] = field(default_factory=set, init=False, repr=False)

    def clear_cache(self) -> None:
        self._cached = None
        self._receiver_sig = None

    def _build(self) -> list[DesignPatch]:
        blits: list[DesignPatch] = []

        if not self.overlay_flags.get("hide_wordmark_for_artwork"):
            wx, wy, ww, wh = rect_for_span_at_cell(
                _PIGEON_WORDMARK_SPAN_W,
                _PIGEON_WORDMARK_SPAN_H,
                row_1based=_PIGEON_WORDMARK_ROW,
                col_1based=_PIGEON_WORDMARK_COL,
            )
            blits.append(
                DesignPatch(
                    x=wx,
                    y=wy,
                    w=ww,
                    h=wh,
                    bgra=_pigeon_wordmark_patch_bgra(ww, wh),
                    layer=PATCH_LAYER_WORDMARK,
                )
            )

        bw, bh = self.badge_span
        bx, by, bww, bhh = rect_for_span_at_cell(
            bw, bh, row_1based=self.badge_row, col_1based=self.badge_col
        )
        bww_i = max(1, int(round(bww * AUDIO_CONFIG_SCALE)))
        bhh_i = max(1, int(round(bhh * AUDIO_CONFIG_SCALE)))
        bx_i = bx + (bww - bww_i) // 2
        by_i = by + (bhh - bhh_i) // 2
        sb = self.service_badge
        show = bool(sb.get("show"))
        fname = str(sb.get("filename") or "").strip()
        svc_label = str(sb.get("label") or "").strip()
        if show and (fname or svc_label):
            badge_path = Path(self.assets_dir) / fname if fname else None
            has_img = bool(fname and badge_path and badge_path.is_file())
            if fname and not has_img and fname not in self._warned_badge_files:
                self._warned_badge_files.add(fname)
                import sys

                sys.stderr.write(f"pigeon: streaming badge not found — {badge_path}\n")
            if has_img and svc_label:
                h_img = max(1, int(round(bhh_i * 0.68)))
                h_txt = max(1, bhh_i - h_img)
                raw = load_image_bgra(badge_path)
                if raw is not None:
                    patch = _image_contain_center_bgra(raw, bww_i, h_img)
                    blits.append(
                        DesignPatch(
                            x=bx_i,
                            y=by_i,
                            w=bww_i,
                            h=h_img,
                            bgra=patch,
                            layer=PATCH_LAYER_STREAMING_BADGE,
                        )
                    )
                lbl = _text_patch_bgra(
                    svc_label,
                    bww_i,
                    h_txt,
                    align="center",
                    fill_rgba=(220, 220, 220, 255),
                )
                blits.append(
                    DesignPatch(
                        x=bx_i,
                        y=by_i + h_img,
                        w=bww_i,
                        h=h_txt,
                        bgra=lbl,
                        layer=PATCH_LAYER_STREAMING_BADGE,
                    )
                )
            elif has_img:
                raw = load_image_bgra(badge_path)
                if raw is not None:
                    patch = _image_contain_center_bgra(raw, bww_i, bhh_i)
                    blits.append(
                        DesignPatch(
                            x=bx_i,
                            y=by_i,
                            w=bww_i,
                            h=bhh_i,
                            bgra=patch,
                            layer=PATCH_LAYER_STREAMING_BADGE,
                        )
                    )
            elif svc_label:
                lbl = _text_patch_bgra(
                    svc_label,
                    bww_i,
                    bhh_i,
                    align="center",
                    fill_rgba=(240, 240, 240, 255),
                )
                blits.append(
                    DesignPatch(
                        x=bx_i,
                        y=by_i,
                        w=bww_i,
                        h=bhh_i,
                        bgra=lbl,
                        layer=PATCH_LAYER_STREAMING_BADGE,
                    )
                )

        ax, ay, aww, ahh = rect_for_span_at_cell(
            self.audio_span_wide,
            1,
            row_1based=self.audio_row,
            col_1based=self.audio_col,
        )
        aw_i = max(1, int(round(aww * AUDIO_CONFIG_SCALE)))
        ah_i = max(1, int(round(ahh * AUDIO_CONFIG_SCALE)))
        ax_i = ax + (aww - aw_i) // 2
        ay_i = ay + (ahh - ah_i) // 2
        rs = self.receiver_state
        line1 = _receiver_audio_display_line(rs.get("incoming"))
        line2 = _receiver_audio_display_line(rs.get("config"))
        line3 = _receiver_volume_display_line(rs.get("volume"))
        texts: list[str] = []
        if line1:
            texts.append(line1)
        if line2:
            texts.append(line2)
        if line3:
            texts.append(line3)
        if texts:
            n = len(texts)
            base = ah_i // n
            rem = ah_i % n
            heights: list[int] = []
            for i in range(n):
                hi = base + (1 if i < rem else 0)
                heights.append(max(1, hi))
            delta = ah_i - sum(heights)
            if delta and heights:
                heights[-1] = max(1, heights[-1] + delta)
            # One short line in a tall slot can over-scale glyphs; keep cap ~one original row.
            audio_text_fit_cap = max(4, ah_i // 3)
            y_off = ay_i
            for text, h in zip(texts, heights):
                blits.append(
                    DesignPatch(
                        x=ax_i,
                        y=y_off,
                        w=aw_i,
                        h=h,
                        bgra=_text_patch_bgra(
                            text,
                            aw_i,
                            h,
                            align="left",
                            fit_max_h=audio_text_fit_cap,
                        ),
                        layer=PATCH_LAYER_RECEIVER_AUDIO,
                    )
                )
                y_off += h

        return blits

    def design_blits(self) -> list[DesignPatch]:
        sb = self.service_badge
        hide_wm = bool(self.overlay_flags.get("hide_wordmark_for_artwork"))
        sig = (
            str(self.receiver_state.get("incoming", "")),
            str(self.receiver_state.get("config", "")),
            str(self.receiver_state.get("volume", "")),
            bool(sb.get("show")),
            str(sb.get("filename") or ""),
            str(sb.get("label") or ""),
            hide_wm,
        )
        if self._cached is not None and self._receiver_sig == sig:
            return self._cached
        self._receiver_sig = sig
        self._cached = self._build()
        return self._cached

    def render(self, canvas_bgr: np.ndarray, *, idle_dim_strength: float = 0.0) -> None:
        ch, cw = canvas_bgr.shape[:2]
        badge_scale = 1.0 + (IDLE_DIM_STREAMING_BADGE_RGB_SCALE - 1.0) * float(
            max(0.0, min(1.0, idle_dim_strength))
        )
        for p in self.design_blits():
            x, y, w, h = p.x, p.y, p.w, p.h
            if w < 1 or h < 1:
                continue
            x0 = max(0, x)
            y0 = max(0, y)
            x1 = min(cw, x + w)
            y1 = min(ch, y + h)
            if x0 >= x1 or y0 >= y1:
                continue
            sx0 = x0 - x
            sy0 = y0 - y
            roi = canvas_bgr[y0:y1, x0:x1]
            patch = p.bgra[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
            if p.layer == PATCH_LAYER_STREAMING_BADGE and badge_scale < 0.999:
                patch = scale_bgra_rgb(patch, badge_scale)
            roi[:] = alpha_blend_bgra_over_bgr(roi, patch)


# Backward-compatible name used by pigeon_0_5 and docs.
PlaybackOverlayWidget = AudioConfig
