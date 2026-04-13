"""
Mic-driven **10-band EQ** visualizer (additive “video” layer under UI / clock saver).

Used when no TMDb / app-logo backdrop scene is active. Fails closed if sounddevice is
unavailable or the stream cannot be opened.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[misc, assignment]

_SAMPLERATE = 44100
# Smaller blocks ≈ lower capture latency vs 1024 (~23 ms → ~12 ms @ 44.1 kHz).
_BLOCK = 512
_CHANNELS = 1
# Lower = snappier bar motion (more jitter); tuned for responsiveness.
_SMOOTH = 0.26
_FFT_EPS = 1e-9
_N_EQ = 10
# Log-spaced audio bands from ~60 Hz up to ~14 kHz (within Nyquist).
_EQ_F_LO_HZ = 60.0
_EQ_F_HI_HZ = 14000.0
# Idle / noise floor: minimum bar height in **design grid rows** (lower = less idle “hiss” height).
_MIN_ROWS_TALL = 1.25
# Strength of additive blend (scaled further by ``intensity_scale`` when set).
_EQ_ADD_GAIN = 0.42
# Per-band FFT magnitudes → log domain; higher = more lift when bins are quiet.
_LOG1P_SPEC_GAIN = 10.0
# RMS → overall envelope: higher pre-gain, lower power, and a slightly higher floor
# make the EQ react more at low listening / mic levels without changing the loud cap much.
_RMS_ENV_PRE = 38.0
_RMS_ENV_PWR = 0.48
_RMS_ENV_FLOOR = 0.055
_RMS_ENV_CEIL = 1.42
# RMS below this (after float32 mic scaling) is treated as silence for the envelope so room noise
# does not constantly pump the overall level.
_RMS_NOISE_GATE = 0.0038
# Subtract from each smoothed log-band value before peak-normalize; knocks down uniform ambient hiss.
_BAND_SMOOTH_TRIM = 0.24
# Normalize bands to peak: smaller divisor + lower gamma = quieter bands read taller vs peak.
_BAND_PEAK_DIV = 1.62
# Slightly higher gamma = less boost for weak bands vs peak (quieter room noise between hits).
_BAND_LVL_GAMMA = 0.78

# Top-of-bar highlight: in the **top 30%** of the drawn bar (by height), blend from base color at
# 70% up from the bar bottom to (1 + tip_boost) at the bar top. ``tip_boost`` follows **level** (0–1 = ``lvls[i]``):
# 0% at 70%, 1% at 71%, linear to 50% at 90%, then 50% through 100%.
_TIP_BOOST_LEVEL_LO = 0.70
_TIP_BOOST_KNEE = 0.71
_TIP_BOOST_KNEE_VAL = 0.01
_TIP_BOOST_LEVEL_HI = 0.90
_TIP_BOOST_MAX = 0.50
# Gradient occupies the top 30% of the bar (from 70% height from bottom upward).
_BAR_HIGHLIGHT_BOTTOM_FRAC = 0.70

# Peak-hold cap: after ``_PEAK_HOLD_STATIONARY_S`` at full opacity, fades out in place over ``_PEAK_DISSOLVE_S``.
_PEAK_CAP_ROWS = 10
_PEAK_DISSOLVE_S = 1.0
# With v < phv, hold the cap at full opacity this long before the dissolve begins.
_PEAK_HOLD_STATIONARY_S = 1.0

# Landing intro: UI shows first, then EQ fades in while bars rise from the bottom (staggered L→R).
_INTRO_FADE_DELAY_S = 0.12
_INTRO_FADE_DUR_S = 0.55
_INTRO_BAR_DELAY_S = 0.28
_INTRO_BAR_RISE_S = 0.72
_INTRO_BAR_STAGGER_S = 0.038
# After this, intro multipliers are 1.0 (used by UI skip-cache).
MIC_VIZ_INTRO_TOTAL_S = (
    _INTRO_BAR_DELAY_S + (_N_EQ - 1) * _INTRO_BAR_STAGGER_S + _INTRO_BAR_RISE_S + 0.05
)

# Bottom-centered black bar: **kick-band onset** from raw blocks + tempo EMA + short anticipation.
_BEAT_KICK_F_LO_HZ = 52.0
_BEAT_KICK_F_HI_HZ = 220.0
# Onset must exceed slow EMA of half-wave increments × SNR (stricter = fewer false triggers).
_BEAT_ONSET_SNR = 5.35
_BEAT_ONSET_CALM_EMA = 0.988
_BEAT_ONSET_ABS_FLOOR = 1.2e-8
_BEAT_COOLDOWN_MIN_S = 0.26
_BEAT_COOLDOWN_IBI_FRAC = 0.38
_BEAT_MIN_IBI_S = 0.30
_BEAT_MAX_IBI_S = 1.45
_BEAT_IBI_EMA_ALPHA = 0.22
_BEAT_IBI_JUMP_REBUILD_FRAC = 0.34
# After this many in-range intervals, anticipation is allowed.
_BEAT_STABILITY_FOR_ANTICIPATE = 3
# Flash slightly before predicted downbeat (perceived latency compensation).
_BEAT_ANTICIPATE_S = 0.042
_BEAT_ANTICIPATE_ALIGN_SLACK_S = 0.07
_BEAT_ANTICIPATION_VIS_FRAC = 0.62
# Strength (0.4…~1.45) → half-width; maps into ~[0,1] before scaling to pixels.
_BEAT_AMP_STRENGTH_LO = 0.38
_BEAT_AMP_STRENGTH_HI = 1.42
# Invisible between hits: envelope decay time constant (seconds).
_BEAT_BAR_DECAY_S = 0.13
# How fast drawn width eases toward the level (grow / shrink from center).
_BEAT_BAR_VIZ_TAU_S = 0.04
# Strip height at bottom (pixels), clamped to frame size.
_BEAT_BAR_H_MIN = 4
_BEAT_BAR_H_MAX = 16
_BEAT_BAR_H_DIV = 85

_lock = threading.Lock()
# Latest mic block only (overwritten by the audio callback). We **do not** consume-and-clear per UI
# frame — doing that caused false “silence” when the compositor ran faster than ~_BLOCK/_SAMPLERATE,
# which tripped aggressive stream reopens and multi-second freezes.
_mic_latest: list[np.ndarray | None] = [None]
_mic_latest_t: list[float] = [0.0]
_stream: object | None = None
_smooth_eq: list[np.ndarray] = [np.zeros(_N_EQ, dtype=np.float32)]
_peak_hold_lvls: list[np.ndarray] = [np.zeros(_N_EQ, dtype=np.float32)]
_peak_hold_latch_mono: list[np.ndarray] = [np.zeros(_N_EQ, dtype=np.float64)]
_peak_dissolve_t0: list[np.ndarray] = [np.zeros(_N_EQ, dtype=np.float64)]
_peak_cap_frozen: list[np.ndarray] = [np.zeros(_N_EQ, dtype=np.float32)]
_last_open_attempt = 0.0
_open_fail_cooldown_s = 8.0
# True stall: no PortAudio callback for this long (wall clock), not “no pop this frame”.
_AUDIO_STALL_REOPEN_S = 0.55
_STALL_REOPEN_MIN_INTERVAL_S = 3.0
_last_stall_reopen_mono: list[float] = [0.0]
_hanning_by_len: dict[int, np.ndarray] = {}
_eq_acc_key: list[tuple[int, int, int] | None] = [None]
_eq_acc_buf: list[np.ndarray | None] = [None]
_roi_f32_key: list[tuple[int, int] | None] = [None]
_roi_f32_buf: list[np.ndarray | None] = [None]
_beat_prev_kick_e: float = 0.0
_beat_kick_initialized: bool = False
_beat_onset_calm_ema: float = 0.0
_beat_last_fire: float = 0.0
_beat_last_onset_t: float = 0.0
_beat_interval_ema: float = 0.0
_beat_stability: int = 0
_beat_anticip_fired_for_next: float = -1.0
_beat_last_amp01: float = 0.45
_beat_bar_level: float = 0.0
_beat_bar_viz: float = 0.0
_beat_bar_last_mono: float = 0.0


def _hanning_cached(n: int) -> np.ndarray:
    w = _hanning_by_len.get(n)
    if w is None:
        w = np.hanning(n).astype(np.float32)
        _hanning_by_len[n] = w
    return w


def _audio_callback(indata: np.ndarray, _frames: int, _t, _status) -> None:
    mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
    with _lock:
        _mic_latest[0] = mono
        _mic_latest_t[0] = time.monotonic()


def _stop_stream() -> None:
    global _stream
    global _beat_prev_kick_e, _beat_kick_initialized, _beat_onset_calm_ema, _beat_last_fire, _beat_last_onset_t
    global _beat_interval_ema, _beat_stability, _beat_anticip_fired_for_next, _beat_last_amp01
    global _beat_bar_level, _beat_bar_viz, _beat_bar_last_mono
    if _stream is None:
        _beat_prev_kick_e = 0.0
        _beat_kick_initialized = False
        _beat_onset_calm_ema = 0.0
        _beat_last_fire = 0.0
        _beat_last_onset_t = 0.0
        _beat_interval_ema = 0.0
        _beat_stability = 0
        _beat_anticip_fired_for_next = -1.0
        _beat_last_amp01 = 0.45
        _beat_bar_level = 0.0
        _beat_bar_viz = 0.0
        _beat_bar_last_mono = 0.0
        return
    try:
        _stream.stop()  # type: ignore[union-attr]
        _stream.close()  # type: ignore[union-attr]
    except Exception:
        pass
    _stream = None
    with _lock:
        _mic_latest[0] = None
        _mic_latest_t[0] = 0.0
    _peak_hold_lvls[0].fill(0.0)
    _peak_hold_latch_mono[0].fill(0.0)
    _peak_dissolve_t0[0].fill(0.0)
    _peak_cap_frozen[0].fill(0.0)
    _beat_prev_kick_e = 0.0
    _beat_kick_initialized = False
    _beat_onset_calm_ema = 0.0
    _beat_last_fire = 0.0
    _beat_last_onset_t = 0.0
    _beat_interval_ema = 0.0
    _beat_stability = 0
    _beat_anticip_fired_for_next = -1.0
    _beat_last_amp01 = 0.45
    _beat_bar_level = 0.0
    _beat_bar_viz = 0.0
    _beat_bar_last_mono = 0.0


def _reopen_input_stream() -> None:
    global _last_open_attempt
    _stop_stream()
    _last_open_attempt = 0.0
    _sync_stream(True)


def _maybe_reopen_on_audio_stall(now: float) -> None:
    """If callbacks go silent while the stream claims to be open, reopen (rate-limited)."""
    if _stream is None or sd is None:
        return
    if now - _last_stall_reopen_mono[0] < _STALL_REOPEN_MIN_INTERVAL_S:
        return
    with _lock:
        t_cb = float(_mic_latest_t[0])
        has = _mic_latest[0] is not None
    if not has or t_cb <= 0.0:
        return
    if now - t_cb < _AUDIO_STALL_REOPEN_S:
        return
    _last_stall_reopen_mono[0] = now
    _reopen_input_stream()


def _sync_stream(want: bool) -> None:
    """Start the input stream while active; stop when inactive to avoid holding the mic."""
    global _stream, _last_open_attempt
    if not want:
        _stop_stream()
        return
    if sd is None:
        return
    if _stream is not None:
        return
    now = time.monotonic()
    if now - _last_open_attempt < _open_fail_cooldown_s and _last_open_attempt > 0:
        return
    _last_open_attempt = now
    try:
        _stream = sd.InputStream(
            channels=_CHANNELS,
            samplerate=_SAMPLERATE,
            blocksize=_BLOCK,
            dtype="float32",
            callback=_audio_callback,
        )
        _stream.start()  # type: ignore[union-attr]
    except Exception:
        _stream = None


def _viz_max_top_y(frame_h: int) -> int:
    """Topmost Y (inclusive) of the EQ layer: aligns with **top** of design grid row 2 (bars may use row 2)."""
    from pigeon.design import DESIGN_H, get_grid_geometry

    g = get_grid_geometry()
    y_row2_top = g.y0 + g.cell
    y = int(round(y_row2_top * frame_h / float(DESIGN_H)))
    return max(0, min(frame_h - 3, y))


def _min_bar_height_px(frame_h: int) -> int:
    from pigeon.design import DESIGN_H, get_grid_geometry

    g = get_grid_geometry()
    cell_px = g.cell * frame_h / float(DESIGN_H)
    return max(3, int(round(_MIN_ROWS_TALL * cell_px)))


def _eq_bands_from_mono(mono: np.ndarray) -> tuple[np.ndarray, float]:
    """Ten log-spaced magnitude bands (smoothed) and RMS."""
    prev = _smooth_eq[0]
    n = mono.shape[0]
    if n < 256:
        return prev.copy(), 0.0
    x = mono * _hanning_cached(n)
    rms = float(np.sqrt(np.mean(x * x) + _FFT_EPS))
    spec = np.abs(np.fft.rfft(x)).astype(np.float32)
    spec[0] = 0.0
    freqs = np.fft.rfftfreq(n, 1.0 / _SAMPLERATE).astype(np.float32)
    nyq = 0.5 * float(_SAMPLERATE)
    f_hi = min(_EQ_F_HI_HZ, nyq * 0.995)
    edges = np.logspace(np.log10(_EQ_F_LO_HZ), np.log10(f_hi), _N_EQ + 1, dtype=np.float32)
    raw = np.zeros(_N_EQ, dtype=np.float32)
    for i in range(_N_EQ):
        lo, hi = float(edges[i]), float(edges[i + 1])
        mask = (freqs >= lo) & (freqs < hi)
        if i == _N_EQ - 1:
            mask = (freqs >= lo) & (freqs <= freqs[-1])
        if not np.any(mask):
            raw[i] = 0.0
        else:
            raw[i] = float(np.mean(spec[mask]))
    raw = np.log1p(raw * _LOG1P_SPEC_GAIN)
    _smooth_eq[0] = prev * (1.0 - _SMOOTH) + raw * _SMOOTH
    return _smooth_eq[0], rms


def _ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) ** 3


def _intro_fade_amp(elapsed_s: float) -> float:
    u = (elapsed_s - _INTRO_FADE_DELAY_S) / _INTRO_FADE_DUR_S if _INTRO_FADE_DUR_S > 0 else 1.0
    return _ease_out_cubic(u)


def _intro_bar_rise_amp(elapsed_s: float, bar_index: int) -> float:
    t0 = _INTRO_BAR_DELAY_S + bar_index * _INTRO_BAR_STAGGER_S
    u = (elapsed_s - t0) / _INTRO_BAR_RISE_S if _INTRO_BAR_RISE_S > 0 else 1.0
    return _ease_out_cubic(u)


def _eq_color_bgr(i: int) -> tuple[float, float, float]:
    t = i / float(max(1, _N_EQ - 1))
    b = 70.0 + (1.0 - t) * 140.0
    g = 90.0 + t * 150.0
    r = 200.0 + t * 55.0
    return (b, g, r)


def _tip_boost_for_level(level: float) -> float:
    """Extra brightness at bar top (0 … _TIP_BOOST_MAX) from normalized level 0–1."""
    a = float(np.clip(level, 0.0, 1.0))
    if a <= _TIP_BOOST_LEVEL_LO:
        return 0.0
    if a <= _TIP_BOOST_KNEE:
        den = _TIP_BOOST_KNEE - _TIP_BOOST_LEVEL_LO
        return ((a - _TIP_BOOST_LEVEL_LO) / den) * _TIP_BOOST_KNEE_VAL if den > 1e-9 else _TIP_BOOST_KNEE_VAL
    if a >= _TIP_BOOST_LEVEL_HI:
        return _TIP_BOOST_MAX
    span = _TIP_BOOST_LEVEL_HI - _TIP_BOOST_KNEE
    return _TIP_BOOST_KNEE_VAL + (a - _TIP_BOOST_KNEE) / span * (_TIP_BOOST_MAX - _TIP_BOOST_KNEE_VAL)


def _row_brightness_mult(row_from_top: int, bar_eff: int, tip_boost: float) -> float:
    """
    ``row_from_top`` = 0 at visual top of bar, ``bar_eff-1`` at bottom.
    Below ``_BAR_HIGHLIGHT_BOTTOM_FRAC`` of bar height: 1.0; top band: ramp to ``1+tip_boost`` at top.
    """
    if tip_boost <= 1e-6:
        return 1.0
    if bar_eff < 2:
        return 1.0 + tip_boost
    # Fraction from bar bottom: 0 at bottom row, 1 at top row.
    dist_top = float(row_from_top)
    frac_from_bottom = 1.0 - (dist_top / float(max(1, bar_eff - 1)))
    frac_from_bottom = float(np.clip(frac_from_bottom, 0.0, 1.0))
    if frac_from_bottom <= _BAR_HIGHLIGHT_BOTTOM_FRAC:
        return 1.0
    u = (frac_from_bottom - _BAR_HIGHLIGHT_BOTTOM_FRAC) / (1.0 - _BAR_HIGHLIGHT_BOTTOM_FRAC)
    return 1.0 + tip_boost * float(np.clip(u, 0.0, 1.0))


def _acc_eq_buffer(slice_h: int, w: int) -> np.ndarray:
    k = (slice_h, w)
    if _eq_acc_key[0] != k or _eq_acc_buf[0] is None:
        _eq_acc_buf[0] = np.zeros((slice_h, w, 3), dtype=np.float32)
        _eq_acc_key[0] = k
    buf = _eq_acc_buf[0]
    buf.fill(0.0)
    return buf


def _strength_to_beat_width01(strength: float) -> float:
    """Map raw pulse strength to 0…1 half-span (louder hit → wider bar)."""
    den = max(1e-6, _BEAT_AMP_STRENGTH_HI - _BEAT_AMP_STRENGTH_LO)
    return float(np.clip((strength - _BEAT_AMP_STRENGTH_LO) / den, 0.0, 1.0))


def _kick_band_energy_sq(mono: np.ndarray) -> float:
    """Short-time energy in kick band (linear spectrum; sharp transients)."""
    n = int(mono.shape[0])
    if n < 64:
        return 0.0
    x = np.asarray(mono, dtype=np.float32).ravel() * _hanning_cached(n)
    spec = np.abs(np.fft.rfft(x)).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1.0 / float(_SAMPLERATE)).astype(np.float32)
    m = (freqs >= _BEAT_KICK_F_LO_HZ) & (freqs <= _BEAT_KICK_F_HI_HZ)
    if not np.any(m):
        return 0.0
    v = spec[m]
    return float(np.dot(v, v))


def _onset_excess_to_amp01(onset: float, thresh: float) -> float:
    """Narrow bar for threshold-only hits; scale up only when onset clearly exceeds threshold."""
    ratio = onset / max(thresh, 1e-12)
    excess = max(0.0, ratio - 1.0)
    pseudo = 0.52 + min(0.88, excess * 0.42)
    pseudo = float(np.clip(pseudo, _BEAT_AMP_STRENGTH_LO, _BEAT_AMP_STRENGTH_HI))
    return _strength_to_beat_width01(pseudo)


def _update_beat_bar_level(mono: np.ndarray | None, now: float) -> None:
    global _beat_prev_kick_e, _beat_kick_initialized, _beat_onset_calm_ema, _beat_last_fire, _beat_last_onset_t
    global _beat_interval_ema, _beat_stability, _beat_anticip_fired_for_next, _beat_last_amp01
    global _beat_bar_level, _beat_bar_viz, _beat_bar_last_mono

    dt = 0.0 if _beat_bar_last_mono <= 0.0 else min(0.22, now - _beat_bar_last_mono)
    _beat_bar_last_mono = now
    tau = max(1e-6, _BEAT_BAR_DECAY_S)
    _beat_bar_level *= math.exp(-dt / tau)

    ibi = float(_beat_interval_ema)
    if (
        ibi >= _BEAT_MIN_IBI_S
        and _beat_stability >= _BEAT_STABILITY_FOR_ANTICIPATE
        and _beat_last_onset_t > 0.0
    ):
        next_b = _beat_last_onset_t + ibi
        while next_b + _BEAT_ANTICIPATE_ALIGN_SLACK_S < now:
            next_b += ibi
        if now > next_b + _BEAT_ANTICIPATE_ALIGN_SLACK_S:
            _beat_anticip_fired_for_next = -1.0
        else:
            t_early = next_b - _BEAT_ANTICIPATE_S
            if t_early <= now and _beat_anticip_fired_for_next < next_b - 1e-6:
                aw = max(0.12, _beat_last_amp01 * _BEAT_ANTICIPATION_VIS_FRAC)
                _beat_bar_level = max(_beat_bar_level, aw)
                _beat_anticip_fired_for_next = next_b

    if mono is not None and mono.size >= 64:
        e = _kick_band_energy_sq(mono)
        if not _beat_kick_initialized:
            _beat_prev_kick_e = e
            _beat_kick_initialized = True
        else:
            onset = max(0.0, e - _beat_prev_kick_e)
            _beat_prev_kick_e = e
            calm = _BEAT_ONSET_CALM_EMA
            _beat_onset_calm_ema = _beat_onset_calm_ema * calm + onset * (1.0 - calm)
            thresh = max(_beat_onset_calm_ema * _BEAT_ONSET_SNR, _BEAT_ONSET_ABS_FLOOR)
            refractory = _BEAT_COOLDOWN_MIN_S
            if ibi > 1e-6:
                refractory = max(refractory, _BEAT_COOLDOWN_IBI_FRAC * ibi)
            if onset >= thresh and (now - _beat_last_fire) >= refractory:
                prev_onset_t = _beat_last_onset_t
                _beat_last_fire = now
                amp01 = _onset_excess_to_amp01(onset, thresh)
                _beat_bar_level = max(_beat_bar_level, amp01)
                _beat_last_amp01 = amp01
                _beat_anticip_fired_for_next = -1.0

                if prev_onset_t > 0.0:
                    gap = now - prev_onset_t
                    if _BEAT_MIN_IBI_S <= gap <= _BEAT_MAX_IBI_S:
                        ibi0 = _beat_interval_ema
                        if ibi0 <= 1e-6:
                            _beat_interval_ema = gap
                            _beat_stability = min(16, _beat_stability + 1)
                        else:
                            dev = abs(gap - ibi0) / max(ibi0, 1e-6)
                            if dev > _BEAT_IBI_JUMP_REBUILD_FRAC:
                                _beat_interval_ema = gap
                                _beat_stability = 1
                            else:
                                a = _BEAT_IBI_EMA_ALPHA
                                _beat_interval_ema = (1.0 - a) * ibi0 + a * gap
                                _beat_stability = min(16, _beat_stability + 1)
                    else:
                        _beat_stability = max(0, _beat_stability - 2)
                _beat_last_onset_t = now

    tau_v = max(1e-6, _BEAT_BAR_VIZ_TAU_S)
    alpha = 1.0 - math.exp(-dt / tau_v) if dt > 0.0 else 1.0
    alpha = min(1.0, alpha)
    _beat_bar_viz += (_beat_bar_level - _beat_bar_viz) * alpha


def _draw_beat_black_bar(bgr: np.ndarray, width01: float, h: int, w: int) -> None:
    """Solid black strip at bottom, centered, ``width01`` = half-width as fraction of ``w//2`` (0 = off)."""
    if width01 < 0.015:
        return
    half_span = int(round(float(width01) * float(w // 2)))
    if half_span < 1:
        return
    cx = w // 2
    x0 = max(0, cx - half_span)
    x1 = min(w, cx + half_span)
    if x1 <= x0:
        return
    bh = max(_BEAT_BAR_H_MIN, min(_BEAT_BAR_H_MAX, h // _BEAT_BAR_H_DIV + _BEAT_BAR_H_MIN))
    bh = min(bh, h)
    y0 = h - bh
    bgr[y0:h, x0:x1] = 0


def blend_mic_visualizer(
    bgr: np.ndarray,
    t_mono: float,
    *,
    active: bool,
    intensity_scale: Callable[[], float] | None = None,
    landing_elapsed_s: float | None = None,
    mono_override: np.ndarray | None = None,
) -> None:
    """
    Additive 10-column EQ: each column is one frequency band, bottom-anchored.

    Draws a **black horizontal bar** on **strict kick-band onsets** from the raw mic block (above the EQ).
    After a few stable beats, a short **anticipated** pulse fires just before the predicted next downbeat;
    interval EMA adapts when tempo drifts. Invisible between defined beats.

    The layer may extend from the frame bottom up through **design grid row 2** (ceiling at row 2’s top edge,
    scaled to the current frame height). Draw clock saver **after** this call so glyphs sit on top.

    ``landing_elapsed_s``: seconds since UI bootstrap; drives a short fade-in and bottom-up bar rise.
    When ``None``, intro is skipped (full-strength immediately).

    When ``active`` is False, releases the mic stream and returns.

    ``mono_override``: optional float32 mono samples (≥256) for dev/tests; skips the real mic and
    ``sounddevice`` when provided (see ``testingEnvironments/mic_viz_test.py``).
    """
    del t_mono  # reserved
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        return
    h, w = int(bgr.shape[0]), int(bgr.shape[1])
    if h < 16 or w < _N_EQ:
        return

    if not active:
        _peak_hold_lvls[0].fill(0.0)
        _peak_hold_latch_mono[0].fill(0.0)
        _peak_dissolve_t0[0].fill(0.0)
        _peak_cap_frozen[0].fill(0.0)
        _stop_stream()
        return

    mono_for_beat: np.ndarray | None = None
    if mono_override is not None:
        m = np.asarray(mono_override, dtype=np.float32).ravel()
        if m.size >= 256:
            smooth, rms = _eq_bands_from_mono(m)
            mono_for_beat = m
        else:
            smooth, rms = _smooth_eq[0], 0.0
    else:
        if sd is None:
            return

        _sync_stream(True)
        with _lock:
            latest = _mic_latest[0]
            t_cb = float(_mic_latest_t[0])
            mono = None if latest is None else np.asarray(latest, dtype=np.float32).copy()
        _now_chk = time.monotonic()
        if mono is not None and t_cb > 0.0 and (_now_chk - t_cb) > _AUDIO_STALL_REOPEN_S:
            _maybe_reopen_on_audio_stall(_now_chk)
        if mono is not None:
            smooth, rms = _eq_bands_from_mono(mono)
            mono_for_beat = mono
        else:
            smooth = _smooth_eq[0]
            rms = 0.0

    now = time.monotonic()
    _update_beat_bar_level(mono_for_beat, now)

    y_top = _viz_max_top_y(h)
    slice_h = h - y_top
    if slice_h < 4:
        return

    usable = slice_h
    min_h_px = min(_min_bar_height_px(h), max(3, usable - 1))
    rms_g = max(0.0, float(rms) - _RMS_NOISE_GATE)
    env = float(
        np.clip((rms_g * _RMS_ENV_PRE) ** _RMS_ENV_PWR, _RMS_ENV_FLOOR, _RMS_ENV_CEIL)
    )
    smooth_vis = np.maximum(0.0, np.asarray(smooth, dtype=np.float32) - _BAND_SMOOTH_TRIM)
    peak = float(np.max(smooth_vis))
    if peak < 1e-6:
        lvls = np.zeros(_N_EQ, dtype=np.float32)
    else:
        lvls = np.clip(smooth_vis / (peak * _BAND_PEAK_DIV), 0.0, 1.0) ** _BAND_LVL_GAMMA
    lvls = lvls * env

    acc = _acc_eq_buffer(slice_h, w)
    if landing_elapsed_s is None:
        fade_amp = 1.0
    else:
        fade_amp = _intro_fade_amp(float(landing_elapsed_s))

    viz_scale = 0.88 if intensity_scale is None else float(intensity_scale())
    viz_scale = max(0.0, min(1.6, viz_scale))

    t_blend = time.monotonic()
    latch = _peak_hold_latch_mono[0]
    d0 = _peak_dissolve_t0[0]
    frz = _peak_cap_frozen[0]

    for i in range(_N_EQ):
        x0 = i * w // _N_EQ
        x1 = (i + 1) * w // _N_EQ if i < _N_EQ - 1 else w
        if x1 <= x0:
            continue
        rise_amp = (
            1.0
            if landing_elapsed_s is None
            else _intro_bar_rise_amp(float(landing_elapsed_s), i)
        )
        span = max(0, usable - min_h_px)
        bar_h = min_h_px + int(lvls[i] * span)
        bar_h = max(min_h_px, min(bar_h, usable))
        # Grow from bottom: scale height toward full ``bar_h`` as ``rise_amp`` eases in.
        bar_eff = max(0, int(round(bar_h * rise_amp)))
        bar_eff = min(bar_eff, usable)

        v = float(lvls[i])
        ph = _peak_hold_lvls[0]
        phv = float(ph[i])
        lt = float(latch[i])
        if v >= phv:
            phv = v
            latch[i] = t_blend
            d0[i] = 0.0
            frz[i] = 0.0
        else:
            if t_blend - lt >= _PEAK_HOLD_STATIONARY_S:
                if d0[i] <= 0.0:
                    d0[i] = t_blend
                    frz[i] = phv
                elapsed = t_blend - d0[i]
                dur = max(_PEAK_DISSOLVE_S, 1e-6)
                if elapsed >= dur:
                    phv = v
                    d0[i] = 0.0
                    frz[i] = 0.0
                    latch[i] = t_blend
                else:
                    phv = float(frz[i])
            else:
                d0[i] = 0.0
                frz[i] = 0.0
        ph[i] = phv

        cb, cg, cr = _eq_color_bgr(i)
        base_gain = _EQ_ADD_GAIN * fade_amp
        tip_boost = _tip_boost_for_level(v)
        r0 = usable - bar_eff if bar_eff >= 1 else usable

        if bar_eff >= 1:
            for rr in range(r0, usable):
                row_from_top = rr - r0
                rm = _row_brightness_mult(row_from_top, bar_eff, tip_boost)
                acc[rr, x0:x1, 0] += cb * base_gain * rm
                acc[rr, x0:x1, 1] += cg * base_gain * rm
                acc[rr, x0:x1, 2] += cr * base_gain * rm

        pk_h = min_h_px + int(phv * span)
        pk_h = max(min_h_px, min(pk_h, usable))
        pk_eff = max(0, int(round(pk_h * rise_amp)))
        pk_eff = min(pk_eff, usable)
        tip_phv = _tip_boost_for_level(phv)
        fade_pk = 1.0
        if d0[i] > 0.0:
            fade_pk = max(0.0, 1.0 - (t_blend - d0[i]) / max(_PEAK_DISSOLVE_S, 1e-6))
        if pk_eff >= 1 and fade_pk > 1e-4:
            p_r0 = usable - pk_eff
            p_top = max(0, p_r0)
            p_end = min(p_top + _PEAK_CAP_ROWS, usable)
            for pr in range(p_top, p_end):
                rm_pk = _row_brightness_mult(pr - p_r0, pk_eff, tip_phv)
                if bar_eff >= 1 and r0 <= pr < usable:
                    rm_br = _row_brightness_mult(pr - r0, bar_eff, tip_boost)
                    d = base_gain * (rm_pk - rm_br) * fade_pk
                else:
                    d = base_gain * rm_pk * fade_pk
                acc[pr, x0:x1, 0] += cb * d
                acc[pr, x0:x1, 1] += cg * d
                acc[pr, x0:x1, 2] += cr * d

    acc *= viz_scale

    rk = (slice_h, w)
    if _roi_f32_key[0] != rk or _roi_f32_buf[0] is None:
        _roi_f32_buf[0] = np.empty((slice_h, w, 3), dtype=np.float32)
        _roi_f32_key[0] = rk
    roi = _roi_f32_buf[0]
    roi[:] = bgr[y_top:h, :, :].astype(np.float32)
    np.add(roi, acc, out=roi)
    np.clip(roi, 0.0, 255.0, out=roi)
    bgr[y_top:h, :, :] = roi.astype(np.uint8)

    _draw_beat_black_bar(bgr, _beat_bar_viz * fade_amp, h, w)
