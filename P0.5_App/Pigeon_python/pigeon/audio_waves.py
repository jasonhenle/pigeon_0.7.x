"""
Mic-driven **10-band EQ** visualizer (additive “video” layer under UI / clock saver).

Used when no TMDb / app-logo backdrop scene is active. Fails closed if sounddevice is
unavailable or the stream cannot be opened.
"""

from __future__ import annotations

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
# Idle / noise floor: bars stay about this many **design grid rows** tall at minimum.
_MIN_ROWS_TALL = 2.5
# Strength of additive blend (scaled further by ``intensity_scale`` when set).
_EQ_ADD_GAIN = 0.42

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

_lock = threading.Lock()
_audio_blocks: list[np.ndarray] = []
_stream: object | None = None
_smooth_eq: list[np.ndarray] = [np.zeros(_N_EQ, dtype=np.float32)]
_last_open_attempt = 0.0
_open_fail_cooldown_s = 8.0
# Re-open the mic if many compositor ticks pass with no new audio (stalled PortAudio / device glitch).
_SILENT_POP_THRESHOLD = 80
_silent_pop_streak: list[int] = [0]
_hanning_by_len: dict[int, np.ndarray] = {}
_eq_acc_key: list[tuple[int, int, int] | None] = [None]
_eq_acc_buf: list[np.ndarray | None] = [None]
_roi_f32_key: list[tuple[int, int] | None] = [None]
_roi_f32_buf: list[np.ndarray | None] = [None]


def _hanning_cached(n: int) -> np.ndarray:
    w = _hanning_by_len.get(n)
    if w is None:
        w = np.hanning(n).astype(np.float32)
        _hanning_by_len[n] = w
    return w


def _audio_callback(indata: np.ndarray, _frames: int, _t, _status) -> None:
    mono = np.asarray(indata[:, 0], dtype=np.float32).copy()
    with _lock:
        _audio_blocks.append(mono)
        # Drop backlog so a slow UI thread does not visualize audio from hundreds of ms ago.
        while len(_audio_blocks) > 2:
            _audio_blocks.pop(0)


def _stop_stream() -> None:
    global _stream
    if _stream is None:
        return
    try:
        _stream.stop()  # type: ignore[union-attr]
        _stream.close()  # type: ignore[union-attr]
    except Exception:
        pass
    _stream = None


def _reopen_input_stream() -> None:
    global _last_open_attempt
    _stop_stream()
    _silent_pop_streak[0] = 0
    _last_open_attempt = 0.0
    _sync_stream(True)


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


def _pop_audio_mono() -> np.ndarray | None:
    """Return only the **latest** mic block; older queued blocks are dropped (minimizes visual lag)."""
    with _lock:
        if not _audio_blocks:
            return None
        mono = np.asarray(_audio_blocks[-1], dtype=np.float32).copy()
        _audio_blocks.clear()
    return mono


def _viz_max_top_y(frame_h: int) -> int:
    """Topmost Y (inclusive) of the EQ layer: aligns with bottom of design grid row 2."""
    from pigeon.design import DESIGN_H, get_grid_geometry

    g = get_grid_geometry()
    y_row2_bottom = g.y0 + 2 * g.cell
    y = int(round(y_row2_bottom * frame_h / float(DESIGN_H)))
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
    raw = np.log1p(raw * 8.0)
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


def _acc_eq_buffer(slice_h: int, w: int) -> np.ndarray:
    k = (slice_h, w)
    if _eq_acc_key[0] != k or _eq_acc_buf[0] is None:
        _eq_acc_buf[0] = np.zeros((slice_h, w, 3), dtype=np.float32)
        _eq_acc_key[0] = k
    buf = _eq_acc_buf[0]
    buf.fill(0.0)
    return buf


def blend_mic_visualizer(
    bgr: np.ndarray,
    t_mono: float,
    *,
    active: bool,
    intensity_scale: Callable[[], float] | None = None,
    landing_elapsed_s: float | None = None,
) -> None:
    """
    Additive 10-column EQ: each column is one frequency band, bottom-anchored.

    The layer may extend from the frame bottom up to the bottom edge of **design grid row 2**
    (scaled to the current frame height). Draw clock saver **after** this call so glyphs sit on top.

    ``landing_elapsed_s``: seconds since UI bootstrap; drives a short fade-in and bottom-up bar rise.
    When ``None``, intro is skipped (full-strength immediately).

    When ``active`` is False, releases the mic stream and returns.
    """
    del t_mono  # reserved
    if bgr.ndim != 3 or bgr.shape[2] != 3:
        return
    h, w = int(bgr.shape[0]), int(bgr.shape[1])
    if h < 16 or w < _N_EQ:
        return

    if not active:
        _stop_stream()
        return

    if sd is None:
        return

    _sync_stream(True)
    mono = _pop_audio_mono()
    if mono is not None:
        _silent_pop_streak[0] = 0
        smooth, rms = _eq_bands_from_mono(mono)
    else:
        _silent_pop_streak[0] += 1
        if (
            _stream is not None
            and _silent_pop_streak[0] >= _SILENT_POP_THRESHOLD
        ):
            _reopen_input_stream()
            mono = _pop_audio_mono()
            if mono is not None:
                _silent_pop_streak[0] = 0
                smooth, rms = _eq_bands_from_mono(mono)
            else:
                smooth = _smooth_eq[0]
                rms = 0.0
        else:
            smooth = _smooth_eq[0]
            rms = 0.0

    y_top = _viz_max_top_y(h)
    slice_h = h - y_top
    if slice_h < 4:
        return

    usable = slice_h
    min_h_px = min(_min_bar_height_px(h), max(3, usable - 1))
    env = float(np.clip((rms * 24.0) ** 0.58, 0.12, 1.38))
    peak = float(np.max(smooth) + 1e-5)
    lvls = np.clip(smooth / (peak * 1.85), 0.0, 1.0) ** 0.78
    lvls = lvls * env

    acc = _acc_eq_buffer(slice_h, w)
    if landing_elapsed_s is None:
        fade_amp = 1.0
    else:
        fade_amp = _intro_fade_amp(float(landing_elapsed_s))

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
        if bar_eff < 1:
            continue
        bar_eff = min(bar_eff, usable)
        r0 = usable - bar_eff
        cb, cg, cr = _eq_color_bgr(i)
        acc[r0:usable, x0:x1, 0] += cb * _EQ_ADD_GAIN * fade_amp
        acc[r0:usable, x0:x1, 1] += cg * _EQ_ADD_GAIN * fade_amp
        acc[r0:usable, x0:x1, 2] += cr * _EQ_ADD_GAIN * fade_amp

    scale = 0.88 if intensity_scale is None else float(intensity_scale())
    scale = max(0.0, min(1.6, scale))
    acc *= scale

    rk = (slice_h, w)
    if _roi_f32_key[0] != rk or _roi_f32_buf[0] is None:
        _roi_f32_buf[0] = np.empty((slice_h, w, 3), dtype=np.float32)
        _roi_f32_key[0] = rk
    roi = _roi_f32_buf[0]
    roi[:] = bgr[y_top:h, :, :]
    np.add(roi, acc, out=roi)
    np.clip(roi, 0.0, 255.0, out=roi)
    bgr[y_top:h, :, :] = roi.astype(np.uint8)
