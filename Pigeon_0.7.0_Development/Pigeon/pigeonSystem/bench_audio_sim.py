"""Benchmark per-frame rebuild cost of NowPlayingScreenWidget with audio sim on."""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

from pigeon.widgets.now_playing_screen import NowPlayingScreenWidget


def main() -> None:
    w = NowPlayingScreenWidget(assets_dir=Path("../pigeonAssets"))
    w.update_state(
        progress=0.42,
        remaining_text="1:23",
        played_text="0:42",
        incoming_audio="Dolby Atmos",
        playback_config="5.1.2",
        volume_text="-32.5 dB",
        has_now_playing=True,
        has_receiver=True,
        has_tmdb=True,
        audio_analysis=True,
        badge_show=True,
        badge_label="netflix",
        audio_levels_sim=True,
        layout_mode="full",
        service_badge_bgra=None,
        tmdb_tt_bgra=None,
        tmdb_backdrop_bgr=None,
    )

    # Warm up: first frame pays SVG rasterization etc.
    t0 = time.perf_counter()
    frame = w.bgra_frame()
    t1 = time.perf_counter()
    assert frame is not None, "bgra_frame returned None"
    print(f"first frame (cold): {(t1 - t0) * 1000:.1f} ms  shape={frame.shape}")

    # Steady state: force cache miss each iteration by clearing the frame cache
    # (equivalent to the 1/30 s _state_sig bucket advancing).
    times = []
    n = 60
    for _ in range(n):
        w._cached_sig = None
        t0 = time.perf_counter()
        frame = w.bgra_frame()
        t1 = time.perf_counter()
        assert frame is not None
        times.append((t1 - t0) * 1000.0)

    times.sort()
    print(
        f"steady-state rebuild over {n} frames: "
        f"median={statistics.median(times):.1f} ms  "
        f"mean={statistics.fmean(times):.1f} ms  "
        f"p95={times[int(n * 0.95) - 1]:.1f} ms  max={times[-1]:.1f} ms"
    )
    budget = 33.0
    med = statistics.median(times)
    print(f"budget 33 ms/frame @30fps: {'OK' if med < budget else 'TOO SLOW'} ({med / budget * 100:.0f}% of budget)")


if __name__ == "__main__":
    sys.exit(main())
