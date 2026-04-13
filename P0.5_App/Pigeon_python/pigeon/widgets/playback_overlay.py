"""Playback chrome: streaming badge, receiver-driven audio lines (grid-aligned)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.design import rect_for_span_at_cell, rect_for_span_top_right_at_cell
from pigeon.font_paths import resolve_ui_font_bold, resolve_ui_font_extrabold
from pigeon.image_ui_protocol import load_image_bgra
from pigeon.widgets.status_bar import DesignPatch

# Service badge + audio lines (audioConfig): 10% smaller, centered in grid cells.
AUDIO_CONFIG_SCALE = 0.9

# Volume line: fixed cap as a fraction of the cell height (no size animation).
VOLUME_TEXT_FIT_H = 0.52
# Volume glyph opacity vs other overlay text (0–255 alpha).
_VOLUME_TEXT_RGBA = (255, 255, 255, 128)

PATCH_LAYER_WORDMARK = "wordmark"  # legacy layer id; wordmark blit removed
PATCH_LAYER_STREAMING_BADGE = "streaming_badge"
PATCH_LAYER_RECEIVER_AUDIO = "receiver_audio"
PATCH_LAYER_PAUSED_ROW = "paused_row"

# Row 6.5 (aligned with TRT pill row); 17 cells wide cols 2–18, centered text.
_PAUSED_ROW_TEXT = "paused"
_PAUSED_ROW_SPAN_W = 17
_PAUSED_ROW_SPAN_H = 1
_PAUSED_ROW_GRID_ROW = 6.5
_PAUSED_ROW_GRID_COL = 2
# Nudge in design pixels (negative = toward top of canvas).
_PAUSED_ROW_OFFSET_Y_PX = -4
_PAUSED_ROW_RGBA = (238, 240, 245, 242)


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


def volume_percent_to_widget_line(value: object) -> str:
    """Map a player-reported level (0–100) to a short overlay string, or ``\"\"`` if unknown."""
    if value is None:
        return ""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if f != f:  # NaN
        return ""
    p = int(max(0, min(100, round(f))))
    return str(p)


def _denon_volume_as_widget_line(effective: str) -> str:
    """Normalize Denon-style strings for the volume row (mute → 0, ``NN%`` → ``NN``)."""
    s = str(effective or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in ("mute", "muted", "off"):
        return "0"
    m = re.search(r"(\d{1,3})\s*%", s)
    if m:
        n = int(m.group(1))
        if 0 <= n <= 100:
            return str(n)
    return s


def compose_playback_volume_widget_line(
    *,
    stream_row: dict[str, object] | None,
    apple_tv_last_metadata: dict[str, object] | None,
    denon_vol_effective: str,
    roku_tv_volume_percent: str,
) -> str:
    """
    Volume widget text: **0 = silent, 100 = max** when the player exposes a 0–100 level
    (Apple TV via pyatv, Roku TV via device-info). Otherwise falls back to usable Denon text.
    """
    from pigeon.app_state import row_is_playback_apple_tv

    is_apple = bool(stream_row) and row_is_playback_apple_tv(stream_row)
    if is_apple:
        md = apple_tv_last_metadata if isinstance(apple_tv_last_metadata, dict) else None
        vp = volume_percent_to_widget_line(md.get("volume_percent") if md else None)
        if vp:
            return vp
    else:
        tv = str(roku_tv_volume_percent or "").strip()
        if tv.isdigit() and 0 <= int(tv) <= 100:
            return tv

    denon_line = _denon_volume_as_widget_line(denon_vol_effective)
    if denon_line and _receiver_volume_display_line(denon_line):
        return denon_line
    return ""


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


def pigeon_wordmark_design_patch() -> DesignPatch:
    """Faint ``pigeon`` wordmark; same placement as in ``AudioConfig`` when wordmark is shown."""
    wx, wy, ww, wh = rect_for_span_at_cell(
        _PIGEON_WORDMARK_SPAN_W,
        _PIGEON_WORDMARK_SPAN_H,
        row_1based=_PIGEON_WORDMARK_ROW,
        col_1based=_PIGEON_WORDMARK_COL,
    )
    return DesignPatch(
        x=wx,
        y=wy,
        w=ww,
        h=wh,
        bgra=_pigeon_wordmark_patch_bgra(ww, wh),
        layer=PATCH_LAYER_WORDMARK,
    )


def _text_patch_bgra(
    text: str,
    w: int,
    h: int,
    *,
    align: str = "left",
    fill_rgba: tuple[int, int, int, int] = (255, 255, 255, 255),
    fit_max_h: int | None = None,
    edge_pad_px: int | None = None,
) -> np.ndarray:
    if w < 2 or h < 2 or not text:
        return np.zeros((h, w, 4), dtype=np.uint8)
    if edge_pad_px is not None:
        pad = max(0, int(edge_pad_px))
    else:
        pad = max(2, int(round(min(w, h) * 0.06)))
    mw, mh = max(4, w - 2 * pad), max(4, h - 2 * pad)
    if fit_max_h is not None:
        mh = min(mh, max(4, fit_max_h))
    font = _fit_font_to_box(text, mw, mh)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Prefer anchor-based placement (Pillow 8+) so the left edge of ink matches ``pad`` for "left".
    try:
        if align == "right":
            draw.text(
                (w - pad, h // 2),
                text,
                font=font,
                fill=fill_rgba,
                anchor="rm",
            )
        elif align == "center":
            draw.text(
                (w // 2, h // 2),
                text,
                font=font,
                fill=fill_rgba,
                anchor="mm",
            )
        else:
            draw.text(
                (pad, h // 2),
                text,
                font=font,
                fill=fill_rgba,
                anchor="lm",
            )
    except (TypeError, ValueError):
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
    Near top row: streaming-service badge with **top-right** on grid (default row 0.5, col 17); 2-wide sits in cols 16–17;
    up to three audio lines from ``receiver_state``
    (incoming, surround/config, volume); placeholder/unknown values are omitted.
    Volume is drawn at grid cell ``[volume_anchor_row, volume_anchor_col]`` (1-based; rows may be fractional).
    Left-aligned in the span (``edge_pad_px=0``); fixed type size (no boost animation).
    Incoming and config stay in the right-aligned strip on ``audio_row``.
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
        default_factory=lambda: {"show_paused_row": False, "clock_saver_volume_only": False}
    )
    badge_row: int | float = 0.5
    # Right edge of the badge aligns with the right edge of grid column 17 (see ``rect_for_span_top_right_at_cell``).
    badge_top_right_col_1based: float = 18.0
    badge_span: tuple[int, int] = (2, 1)
    audio_row: int | float = 7.5
    audio_span_wide: int = 4
    # Volume: top-left at grid [row, col] = [7.5, 3].
    volume_anchor_row: int | float = 7.5
    volume_anchor_col: int = 3
    volume_span_wide: int = 5
    volume_span_tall: int = 1

    _cached: list[DesignPatch] | None = field(default=None, repr=False)
    _receiver_sig: tuple[object, ...] | None = field(default=None, init=False, repr=False)
    _warned_badge_files: set[str] = field(default_factory=set, init=False, repr=False)

    def clear_cache(self) -> None:
        self._cached = None
        self._receiver_sig = None

    def _volume_patch(self) -> DesignPatch | None:
        line3 = _receiver_volume_display_line(self.receiver_state.get("volume"))
        if not line3:
            return None
        vx, vy, vw, vh = rect_for_span_at_cell(
            self.volume_span_wide,
            self.volume_span_tall,
            row_1based=self.volume_anchor_row,
            col_1based=self.volume_anchor_col,
        )
        fit_h = max(4, int(round(vh * VOLUME_TEXT_FIT_H)))
        return DesignPatch(
            x=vx,
            y=vy,
            w=vw,
            h=vh,
            bgra=_text_patch_bgra(
                line3,
                vw,
                vh,
                align="left",
                fill_rgba=_VOLUME_TEXT_RGBA,
                fit_max_h=fit_h,
                edge_pad_px=0,
            ),
            layer=PATCH_LAYER_RECEIVER_AUDIO,
        )

    def _build(self) -> list[DesignPatch]:
        if self.overlay_flags.get("clock_saver_volume_only"):
            vp = self._volume_patch()
            return [vp] if vp is not None else []

        blits: list[DesignPatch] = []

        if self.overlay_flags.get("show_paused_row"):
            px, py, pw, ph = rect_for_span_at_cell(
                _PAUSED_ROW_SPAN_W,
                _PAUSED_ROW_SPAN_H,
                row_1based=_PAUSED_ROW_GRID_ROW,
                col_1based=_PAUSED_ROW_GRID_COL,
            )
            py = max(0, py + _PAUSED_ROW_OFFSET_Y_PX)
            blits.append(
                DesignPatch(
                    x=px,
                    y=py,
                    w=pw,
                    h=ph,
                    bgra=_text_patch_bgra(
                        _PAUSED_ROW_TEXT,
                        pw,
                        ph,
                        align="center",
                        fill_rgba=_PAUSED_ROW_RGBA,
                    ),
                    layer=PATCH_LAYER_PAUSED_ROW,
                )
            )

        bw, bh = self.badge_span
        bx, by, bww, bhh = rect_for_span_top_right_at_cell(
            bw,
            bh,
            row_1based=self.badge_row,
            col_right_1based=float(self.badge_top_right_col_1based),
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
            has_file = bool(fname and badge_path and badge_path.is_file())
            if fname and not has_file and fname not in self._warned_badge_files:
                self._warned_badge_files.add(fname)
                import sys

                sys.stderr.write(f"pigeon: streaming badge not found — {badge_path}\n")
            img_bgra = load_image_bgra(badge_path) if has_file else None
            if has_file and img_bgra is None and fname not in self._warned_badge_files:
                self._warned_badge_files.add(fname)
                import sys

                sys.stderr.write(f"pigeon: streaming badge unreadable — {badge_path}\n")

            # Prefer logo image alone when the asset loads; text is fallback when there is no file.
            if img_bgra is not None:
                patch = _image_contain_center_bgra(img_bgra, bww_i, bhh_i)
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

        # Right edge of the audio strip aligns with the right edge of grid column 17 (col_right_1based=18).
        ax, ay, aww, ahh = rect_for_span_top_right_at_cell(
            self.audio_span_wide,
            1,
            row_1based=self.audio_row,
            col_right_1based=18,
        )
        aw_i = max(1, int(round(aww * AUDIO_CONFIG_SCALE)))
        ah_i = max(1, int(round(ahh * AUDIO_CONFIG_SCALE)))
        ax_i = ax + (aww - aw_i) // 2
        ay_i = ay + (ahh - ah_i) // 2
        rs = self.receiver_state
        line1 = _receiver_audio_display_line(rs.get("incoming"))
        line2 = _receiver_audio_display_line(rs.get("config"))
        non_vol: list[str] = []
        if line1:
            non_vol.append(line1)
        if line2:
            non_vol.append(line2)

        if non_vol:
            n = len(non_vol)
            base = ah_i // n
            rem = ah_i % n
            heights: list[int] = []
            for i in range(n):
                hi = base + (1 if i < rem else 0)
                heights.append(max(1, hi))
            delta = ah_i - sum(heights)
            if delta and heights:
                heights[-1] = max(1, heights[-1] + delta)
            incoming_config_fit_cap = max(4, ah_i // 3)
            y_off = ay_i
            for text, h in zip(non_vol, heights):
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
                            align="right",
                            fit_max_h=incoming_config_fit_cap,
                        ),
                        layer=PATCH_LAYER_RECEIVER_AUDIO,
                    )
                )
                y_off += h

        vp = self._volume_patch()
        if vp is not None:
            blits.append(vp)

        return blits

    def design_blits(self) -> list[DesignPatch]:
        sb = self.service_badge
        show_paused = bool(self.overlay_flags.get("show_paused_row"))
        sig = (
            str(self.receiver_state.get("incoming", "")),
            str(self.receiver_state.get("config", "")),
            str(self.receiver_state.get("volume", "")),
            bool(sb.get("show")),
            str(sb.get("filename") or ""),
            str(sb.get("label") or ""),
            show_paused,
            bool(self.overlay_flags.get("clock_saver_volume_only")),
        )
        if self._cached is not None and self._receiver_sig == sig:
            return self._cached
        self._receiver_sig = sig
        self._cached = self._build()
        return self._cached

    def render(self, canvas_bgr: np.ndarray) -> None:
        ch, cw = canvas_bgr.shape[:2]
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
            roi[:] = alpha_blend_bgra_over_bgr(roi, patch)


# Backward-compatible name used by pigeon_0_5 and docs.
PlaybackOverlayWidget = AudioConfig
