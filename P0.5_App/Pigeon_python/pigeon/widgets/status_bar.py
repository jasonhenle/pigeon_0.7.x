"""Composite status bar: left/right TRT pills + translucent now-playing progress bar (on top).

Elapsed and remaining timecodes use **two** black capsules (played / remaining). The gap under
the progress bar has **no** black pill so only the backdrop shows through the translucent bar.
The bar is the same thickness as a full grid cell and composites **on top**.

The bar track is ~60% gray. Progress is a contrasting accent fill (left → right) sampled
from the TMDb backdrop (vivid / saturated region). The pill-shaped **progress** bar is
mostly opaque (~80%) so backdrop still reads through; the TRT capsules stay fully opaque.
No separate CTI knob.
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

# Progress track + fill alpha (0 = invisible, 1 = opaque). TRT capsules are not affected.
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


def _blend_bgra_over_bgra_inplace(
    dst_bgra: np.ndarray, src_bgra: np.ndarray, x0: int, y0: int
) -> None:
    """Alpha-composite ``src`` onto ``dst`` at top-left (x0, y0) (source-over)."""
    dh, dw = dst_bgra.shape[:2]
    sh, sw = src_bgra.shape[:2]
    if sh < 1 or sw < 1:
        return
    x1, x2 = max(0, x0), min(dw, x0 + sw)
    y1, y2 = max(0, y0), min(dh, y0 + sh)
    if x1 >= x2 or y1 >= y2:
        return
    sx0, sy0 = x1 - x0, y1 - y0
    dst = dst_bgra[y1:y2, x1:x2].astype(np.float32)
    src = src_bgra[sy0 : sy0 + (y2 - y1), sx0 : sx0 + (x2 - x1)].astype(np.float32)
    sa = src[:, :, 3:4] / 255.0
    da = dst[:, :, 3:4] / 255.0
    inv_sa = 1.0 - sa
    out_a = sa + da * inv_sa
    out_rgb = (src[:, :, :3] * sa + dst[:, :, :3] * da * inv_sa) / np.maximum(out_a, 1e-6)
    dst_bgra[y1:y2, x1:x2, :3] = np.clip(out_rgb, 0, 255).astype(np.uint8)
    dst_bgra[y1:y2, x1:x2, 3:4] = np.clip(out_a * 255.0, 0, 255).astype(np.uint8)


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
    """Left/right TRT pills with a clear gap; translucent progress bar on top (z-order)."""

    def __init__(
        self,
        *,
        assets_dir: Path,
        trt_row: int = 8,
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

    @property
    def accent_bgr(self) -> tuple[int, int, int]:
        """BGR tint used for the progress bar (sampled from backdrop, or fallback)."""
        return self._accent_bgr

    def clear_cache(self) -> None:
        self._cached_blits = None

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

        played_w = max(pw, bar_x - px)
        rem_w = max(rw, (rx + rw) - bar_right)
        row_h = max(1, ph, rh)

        bar_y = py
        bar_bh = cell

        py0_p, pbh_p = self._tc_played._pill_metrics_extend_horizontal(
            played_w, row_h, content_width=pw, align="left"
        )
        py0_r, pbh_r = self._tc_remaining._pill_metrics_extend_horizontal(
            rem_w, row_h, content_width=rw, align="right"
        )
        pbh = max(pbh_p, pbh_r)
        pill_y0 = max(0, (row_h - pbh) // 2)
        vcy = pill_y0 + pbh // 2

        # Left and right pills only — no black under the progress bar (backdrop shows through the bar).
        left_bgra = np.zeros((row_h, played_w, 4), dtype=np.uint8)
        left_bgra[pill_y0 : pill_y0 + pbh, :, :] = pill_bgra_black(played_w, pbh)
        played_txt = self._tc_played.bgra_text_only_extend_under_bar(
            canvas_w=played_w,
            canvas_h=row_h,
            content_width=pw,
            align="left",
            cy_center=vcy,
        )
        _blend_bgra_over_bgra_inplace(left_bgra, played_txt, 0, 0)
        blits.append(DesignPatch(x=px, y=py, w=played_w, h=row_h, bgra=left_bgra))

        right_bgra = np.zeros((row_h, rem_w, 4), dtype=np.uint8)
        right_bgra[pill_y0 : pill_y0 + pbh, :, :] = pill_bgra_black(rem_w, pbh)
        rem_txt = self._tc_remaining.bgra_text_only_extend_under_bar(
            canvas_w=rem_w,
            canvas_h=row_h,
            content_width=rw,
            align="right",
            cy_center=vcy,
        )
        _blend_bgra_over_bgra_inplace(right_bgra, rem_txt, 0, 0)
        blits.append(DesignPatch(x=bar_right, y=ry, w=rem_w, h=row_h, bgra=right_bgra))

        if cell >= 1 and bar_bw >= 4 and bar_bh >= 4:
            bar_r = self._build_bar_patch_bgra(bar_bw=bar_bw, bar_bh=bar_bh)
            blits.append(DesignPatch(x=bar_x, y=bar_y, w=bar_bw, h=bar_bh, bgra=bar_r))

        self._cached_blits = blits
        return blits

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
