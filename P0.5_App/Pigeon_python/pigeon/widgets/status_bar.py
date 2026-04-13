"""Composite status bar: one continuous black pill, bar-shaped hole, then labels, then the bar (on top).

The black strip is a **single** unbroken capsule (e.g. cols 3–17). A hole matching the progress
bar’s pill mask (same geometry as :func:`pill_alpha_mask`) is punched through it so the backdrop
shows there; the semi-transparent bar draws on top in the same footprint.

The bar track is ~60% gray. Progress is a contrasting accent fill (left → right) sampled
from the TMDb backdrop (vivid / saturated region). No separate CTI knob.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from pigeon.compositing import alpha_blend_bgra_over_bgr
from pigeon.design import DESIGN_H, DESIGN_W, get_grid_geometry, rect_for_span_at_cell
from pigeon.ui_pill import pill_alpha_mask, pill_bgra_black
from pigeon.widgets.timecode_label import TimecodeLabelWidget

# ~60% luminance neutral (BGR).
_TRACK_GRAY_BGR = (153, 153, 153)

# Fallback accent when no backdrop (vivid orange, BGR).
_ACCENT_FALLBACK_BGR = (40, 140, 255)

# Progress track + fill alpha inside the pill mask (backdrop shows through; black strip does not).
_PROGRESS_BAR_OPACITY = 0.8


@dataclass(frozen=True)
class DesignPatch:
    x: int
    y: int
    w: int
    h: int
    bgra: np.ndarray
    # "default" | "wordmark" | "streaming_badge" | "receiver_audio" (playback overlay).
    layer: str = "default"


def sample_accent_bgr_from_backdrop(bgr: np.ndarray | None) -> tuple[int, int, int]:
    """
    Pick a bold BGR accent from backdrop art: favor high saturation × value pixels,
    then boost saturation/value for on-screen pop.
    """
    if bgr is None or bgr.size == 0 or bgr.ndim != 3 or bgr.shape[2] < 3:
        return _ACCENT_FALLBACK_BGR
    h0, w0 = bgr.shape[:2]
    if h0 < 2 or w0 < 2:
        return _ACCENT_FALLBACK_BGR
    # Center crop avoids letterbox extremes on some masters.
    ym, xm = int(h0 * 0.12), int(w0 * 0.08)
    y1, x1 = h0 - ym, w0 - xm
    crop = bgr[ym:y1, xm:x1]
    if crop.size == 0:
        crop = bgr
    small = cv2.resize(crop, (72, 40), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    _, s, v = cv2.split(hsv)
    s_f = s.astype(np.float32)
    v_f = v.astype(np.float32)
    scores = s_f * v_f
    flat = scores.ravel()
    if flat.size == 0:
        return _ACCENT_FALLBACK_BGR
    # Top 5% by score → robust mean (reduces single-pixel noise).
    k = max(1, int(round(0.05 * flat.size)))
    idx = np.argpartition(flat, -k)[-k:]
    ys, xs = np.unravel_index(idx, scores.shape)
    b_acc = int(np.median(small[ys, xs, 0]))
    g_acc = int(np.median(small[ys, xs, 1]))
    r_acc = int(np.median(small[ys, xs, 2]))
    return _boost_accent_bgr(b_acc, g_acc, r_acc)


def _punch_bar_shaped_hole_in_strip_bgra(
    strip_bgra: np.ndarray,
    *,
    bar_local_x: int,
    bar_local_y: int,
    bar_bw: int,
    bar_bh: int,
) -> None:
    """
    In-place: reduce strip alpha by the progress-bar pill mask so the hole matches the bar shape.

    Uses the same ``pill_alpha_mask(bar_bw, bar_bh)`` as :meth:`StatusBarWidget._build_bar_patch_bgra`.
    """
    uh, uw = strip_bgra.shape[:2]
    if bar_bw < 1 or bar_bh < 1:
        return
    bar_m = pill_alpha_mask(bar_bw, bar_bh).astype(np.float32) / 255.0
    hx0, hy0 = bar_local_x, bar_local_y
    sy0 = max(0, hy0)
    sx0 = max(0, hx0)
    sy1 = min(uh, hy0 + bar_bh)
    sx1 = min(uw, hx0 + bar_bw)
    if sy0 >= sy1 or sx0 >= sx1:
        return
    my0, mx0 = sy0 - hy0, sx0 - hx0
    mh, mw = sy1 - sy0, sx1 - sx0
    m = bar_m[my0 : my0 + mh, mx0 : mx0 + mw]
    sub_a = strip_bgra[sy0:sy1, sx0:sx1, 3].astype(np.float32)
    strip_bgra[sy0:sy1, sx0:sx1, 3] = np.clip(sub_a * (1.0 - m), 0, 255).astype(np.uint8)


def _boost_accent_bgr(b: int, g: int, r: int) -> tuple[int, int, int]:
    px = np.uint8([[[b, g, r]]])
    hsv = cv2.cvtColor(px, cv2.COLOR_BGR2HSV)
    h = int(hsv[0, 0, 0])
    s = min(255, int(hsv[0, 0, 1]) + 55)
    v = min(255, int(hsv[0, 0, 2]) + 45)
    hsv[0, 0, 0] = h
    hsv[0, 0, 1] = s
    hsv[0, 0, 2] = v
    out = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return (int(out[0]), int(out[1]), int(out[2]))


class StatusBarWidget:
    """One continuous black pill with a bar-shaped mask cutout; translucent bar on top."""

    def __init__(
        self,
        *,
        assets_dir: Path,
        trt_row: int | float = 8,
        trt_played_col: int = 3,
        trt_remaining_col: int = 16,  # 2-wide ending col 17 leaves margin before grid edge
        trt_played_text: str = "00:00:00",
        trt_remaining_text: str = "01:00:00",
        trt_label_span_wide: int = 2,
        trt_label_span_tall: int = 1,
    ) -> None:
        self._assets_dir = Path(assets_dir)
        self._tc_played = TimecodeLabelWidget(
            anchor_row=trt_row,
            anchor_col=trt_played_col,
            text=trt_played_text,
            span_wide=trt_label_span_wide,
            span_tall=trt_label_span_tall,
        )
        self._tc_remaining = TimecodeLabelWidget(
            anchor_row=trt_row,
            anchor_col=trt_remaining_col,
            text=trt_remaining_text,
            span_wide=trt_label_span_wide,
            span_tall=trt_label_span_tall,
        )
        self._accent_bgr: tuple[int, int, int] = _ACCENT_FALLBACK_BGR
        self._progress: float = 0.0
        self._cached_blits: list[DesignPatch] | None = None
        # When False, omit TRT pills + progress bar (nothing queued / idle Apple TV).
        self._now_playing_chrome_visible: bool = False
        # When True, omit bar + pills during theater idle-dim (red mono) even if chrome would show.
        self._theater_dim_suppressed: bool = False

    @property
    def accent_bgr(self) -> tuple[int, int, int]:
        """BGR tint used for the progress bar (sampled from backdrop, or fallback)."""
        return self._accent_bgr

    def clear_cache(self) -> None:
        self._cached_blits = None

    def set_now_playing_chrome_visible(self, visible: bool) -> bool:
        """Show or hide the progress bar and timecode pills (compositor uses empty blits when hidden)."""
        v = bool(visible)
        if v == self._now_playing_chrome_visible:
            return False
        self._now_playing_chrome_visible = v
        self.clear_cache()
        return True

    @property
    def now_playing_chrome_visible(self) -> bool:
        return self._now_playing_chrome_visible

    @property
    def theater_dim_suppressed(self) -> bool:
        return self._theater_dim_suppressed

    def set_theater_dim_suppressed(self, suppressed: bool) -> bool:
        """Hide TRT + progress chrome while the UI is in (or easing into) theater idle-dim."""
        v = bool(suppressed)
        if v == self._theater_dim_suppressed:
            return False
        self._theater_dim_suppressed = v
        self.clear_cache()
        return True

    def set_accent_from_backdrop_bgr(self, backdrop_bgr: np.ndarray | None) -> bool:
        """Re-sample accent from TMDb backdrop (call when backdrop image updates)."""
        new_acc = sample_accent_bgr_from_backdrop(backdrop_bgr)
        if new_acc != self._accent_bgr:
            self._accent_bgr = new_acc
            self.clear_cache()
            return True
        return False

    def set_timecodes(self, *, played_text: str | None = None, remaining_text: str | None = None) -> bool:
        return self.set_now_playing_display(
            played_text=played_text, remaining_text=remaining_text, progress=None
        )

    def set_now_playing_display(
        self,
        *,
        played_text: str | None = None,
        remaining_text: str | None = None,
        progress: float | None = None,
    ) -> bool:
        """Update TRT strings and/or progress [0,1]. Pass progress=None to leave fraction unchanged."""
        changed = False
        if played_text is not None:
            changed = self._tc_played.set_text(played_text) or changed
        if remaining_text is not None:
            changed = self._tc_remaining.set_text(remaining_text) or changed
        if progress is not None:
            pf = max(0.0, min(1.0, float(progress)))
            if abs(pf - self._progress) > 1e-9:
                self._progress = pf
                changed = True
        if changed:
            self.clear_cache()
        return changed

    def _build_bar_patch_bgra(
        self,
        *,
        bar_bw: int,
        bar_bh: int,
    ) -> np.ndarray:
        ab, ag, ar = self._accent_bgr
        patch = np.zeros((bar_bh, bar_bw, 4), dtype=np.uint8)

        mask = pill_alpha_mask(bar_bw, bar_bh)

        # Track (grey) everywhere inside mask.
        patch[:, :, 0] = _TRACK_GRAY_BGR[0]
        patch[:, :, 1] = _TRACK_GRAY_BGR[1]
        patch[:, :, 2] = _TRACK_GRAY_BGR[2]
        patch[:, :, 3] = mask

        # Accent fill left->right, still clipped to pill mask.
        frac = max(0.0, min(1.0, float(self._progress)))
        fill_w = int(round(frac * bar_bw))
        fill_w = max(0, min(bar_bw, fill_w))
        if fill_w > 0:
            fill_mask = mask.copy()
            fill_mask[:, fill_w:] = 0
            accent_idx = fill_mask > 0
            patch[accent_idx, 0] = ab
            patch[accent_idx, 1] = ag
            patch[accent_idx, 2] = ar
        op = max(0.0, min(1.0, float(_PROGRESS_BAR_OPACITY)))
        patch[:, :, 3] = (patch[:, :, 3].astype(np.float32) * op).astype(np.uint8)
        return patch

    def design_blits(self) -> list[DesignPatch]:
        if not self._now_playing_chrome_visible or self._theater_dim_suppressed:
            return []
        if self._cached_blits is not None:
            return self._cached_blits

        g = get_grid_geometry(width=DESIGN_W, height=DESIGN_H)
        cell = g.cell
        blits: list[DesignPatch] = []

        sw_p, sh_p = self._tc_played.grid_span
        ar_p, ac_p = self._tc_played.grid_anchor
        px, py, pw, ph = rect_for_span_at_cell(sw_p, sh_p, row_1based=ar_p, col_1based=ac_p)

        sw_r, sh_r = self._tc_remaining.grid_span
        ar_r, ac_r = self._tc_remaining.grid_anchor
        rx, ry, rw, rh = rect_for_span_at_cell(
            sw_r, sh_r, row_1based=ar_r, col_1based=ac_r
        )

        # Black pill geometry first — TRT gutters are [ux, bar_x) and [bar_right, ux+uw).
        pill_col_1based = ac_p
        pill_span_w = (ac_r + sw_r - 1) - pill_col_1based + 1
        ux, uy, uw, uh = rect_for_span_at_cell(
            pill_span_w,
            1,
            row_1based=ar_p,
            col_1based=pill_col_1based,
        )

        gap_left = px + pw
        gap_right = rx
        gap_mid = 0.5 * (gap_left + gap_right)
        gap_w = max(0, gap_right - gap_left)

        # Symmetric padding inside the TRT label gap; bar centered on the gap midpoint.
        inner_pad = int(round(cell * 0.3))
        bar_bw = max(cell * 4, gap_w - 2 * inner_pad)
        bar_bw = min(bar_bw, gap_w)
        bar_x = int(round(gap_mid - bar_bw / 2.0))
        bar_right = bar_x + bar_bw

        bar_y = uy
        bar_bh = cell

        played_slot_w = max(1, bar_x - ux)
        rem_slot_w = max(1, (ux + uw) - bar_right)

        py0_p, pbh_p = self._tc_played._pill_metrics_extend_horizontal(
            played_slot_w, uh, content_width=pw, align="left"
        )
        py0_r, pbh_r = self._tc_remaining._pill_metrics_extend_horizontal(
            rem_slot_w, uh, content_width=rw, align="right"
        )
        pbh = max(pbh_p, pbh_r)
        pill_y0 = max(0, (uh - pbh) // 2)
        vcy = uy + pill_y0 + pbh // 2
        strip_bgra = pill_bgra_black(uw, uh)
        if cell >= 1 and bar_bw >= 4 and bar_bh >= 4:
            _punch_bar_shaped_hole_in_strip_bgra(
                strip_bgra,
                bar_local_x=bar_x - ux,
                bar_local_y=bar_y - uy,
                bar_bw=bar_bw,
                bar_bh=bar_bh,
            )
        blits.append(DesignPatch(x=ux, y=uy, w=uw, h=uh, bgra=strip_bgra))

        played_txt = self._tc_played.bgra_text_only_extend_under_bar(
            canvas_w=played_slot_w,
            canvas_h=uh,
            content_width=pw,
            align="center",
            cy_center=vcy - uy,
        )
        blits.append(DesignPatch(x=ux, y=uy, w=played_slot_w, h=uh, bgra=played_txt))

        rem_txt = self._tc_remaining.bgra_text_only_extend_under_bar(
            canvas_w=rem_slot_w,
            canvas_h=uh,
            content_width=rw,
            align="center",
            cy_center=vcy - uy,
        )
        blits.append(DesignPatch(x=bar_right, y=uy, w=rem_slot_w, h=uh, bgra=rem_txt))

        if cell >= 1 and bar_bw >= 4 and bar_bh >= 4:
            bar_r = self._build_bar_patch_bgra(bar_bw=bar_bw, bar_bh=bar_bh)
            blits.append(DesignPatch(x=bar_x, y=bar_y, w=bar_bw, h=bar_bh, bgra=bar_r))

        self._cached_blits = blits
        return blits

    def render(self, canvas_bgr: np.ndarray) -> None:
        if not self._now_playing_chrome_visible or self._theater_dim_suppressed:
            return
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
