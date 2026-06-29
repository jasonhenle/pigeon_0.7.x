"""Wall-clock-driven ``pigeonStartup.mp4`` decoder for ``startUp.transition``.

During the post-splash choreography window Pigeon swaps the static
``AppLogo_Pigeon.png`` in the pigeonTMDB_TT rect for a looping video clip
(``pigeonAssets/pigeonStartup.mp4``). The file is decoded via OpenCV's
``VideoCapture`` (hardware-accelerated on macOS through AVFoundation) and
the current frame is selected by wall-clock time rather than by polling the
capture — that way a slow compositor never stalls the animation and a fast
compositor never requests the same frame twice. Frames are returned as BGRA
``numpy`` arrays so they can be alpha-blended over the black canvas with
the same helper used for the PNG logo.

The module is deliberately tolerant of missing OpenCV / missing video
files: every public function returns ``None`` on any failure, and the main
render path already short-circuits to the static logo in that case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

try:
    import cv2  # type: ignore[import-not-found]

    _CV2_OK = True
except Exception:
    cv2 = None  # type: ignore[assignment]
    _CV2_OK = False


_STARTUP_VIDEO_FILENAME = "pigeonStartup.mp4"

# One module-level cap + duration cache. The intro runs once per launch so we
# don't bother with multi-file caching.
_CAP_HOLDER: list[object] = [None]  # VideoCapture | None
_CAP_PATH: list[Optional[Path]] = [None]
_CAP_FPS: list[float] = [0.0]
_CAP_FRAMES: list[int] = [0]
_LAST_FRAME_IDX: list[int] = [-1]
_LAST_FRAME_BGRA: list[Optional[np.ndarray]] = [None]


def _find_startup_video(assets_root: Path) -> Optional[Path]:
    """Return the path to ``pigeonStartup.mp4`` under ``assets_root`` or ``None``."""
    if assets_root is None:
        return None
    direct = assets_root / _STARTUP_VIDEO_FILENAME
    if direct.is_file():
        return direct
    # Be generous about casing — different authoring tools have shipped the file
    # as ``pigeonStartup.mp4`` / ``pigeonStartUp.mp4`` / ``PigeonStartup.mp4`` in
    # the past, and macOS's default case-insensitive FS hides which form is on
    # disk. Scan with a case-insensitive compare so any casing resolves.
    try:
        for p in assets_root.iterdir():
            if p.is_file() and p.name.lower() == _STARTUP_VIDEO_FILENAME.lower():
                return p
    except OSError:
        pass
    return None


def _ensure_cap(assets_root: Path) -> bool:
    """Open the capture lazily. Returns ``True`` when a cap is ready."""
    if not _CV2_OK:
        return False
    path = _find_startup_video(assets_root)
    if path is None:
        return False
    if _CAP_HOLDER[0] is not None and _CAP_PATH[0] == path:
        return True
    # Path changed (or first open) — release any stale cap.
    old = _CAP_HOLDER[0]
    if old is not None:
        try:
            old.release()  # type: ignore[attr-defined]
        except Exception:
            pass
        _CAP_HOLDER[0] = None
    try:
        cap = cv2.VideoCapture(str(path))  # type: ignore[union-attr]
    except Exception:
        return False
    if not cap.isOpened():
        try:
            cap.release()
        except Exception:
            pass
        return False
    fps = 0.0
    frames = 0
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS))  # type: ignore[union-attr]
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))  # type: ignore[union-attr]
    except Exception:
        fps = 0.0
        frames = 0
    if fps <= 1.0 or not (fps == fps):  # NaN-safe
        fps = 30.0
    if frames <= 0:
        frames = 0  # Unknown length — we'll fall back to wall-clock with a safe cap.
    _CAP_HOLDER[0] = cap
    _CAP_PATH[0] = path
    _CAP_FPS[0] = fps
    _CAP_FRAMES[0] = frames
    _LAST_FRAME_IDX[0] = -1
    _LAST_FRAME_BGRA[0] = None
    return True


def _frame_to_bgra(frame: np.ndarray) -> Optional[np.ndarray]:
    """Convert a decoded BGR / BGRA frame to BGRA with a fully-opaque alpha."""
    if frame is None or not isinstance(frame, np.ndarray):
        return None
    if frame.ndim != 3:
        return None
    h, w, ch = frame.shape
    if ch == 4:
        return frame
    if ch == 3:
        alpha = np.full((h, w, 1), 255, dtype=np.uint8)
        return np.concatenate([frame, alpha], axis=2)
    return None


def current_startup_bgra_frame(
    assets_root: Path,
    elapsed_s: float,
    *,
    loop: bool = True,
    max_seconds_cap: float = 10.0,
) -> Optional[np.ndarray]:
    """Return the BGRA frame of ``pigeonStartup.mp4`` at ``elapsed_s``.

    ``elapsed_s`` is wall-clock seconds since the startup transition began
    (typically ``now - mic_viz_intro_start_mono[0]``). When ``loop`` is true
    (default) the clip loops after its last frame so it fills the full intro
    window even if the mp4 is shorter. ``max_seconds_cap`` guards against
    absurd elapsed values when the frame count is unknown.

    Returns ``None`` when OpenCV, the file, or frame decode is unavailable.
    """
    if elapsed_s is None or elapsed_s < 0.0:
        elapsed_s = 0.0
    if not _ensure_cap(assets_root):
        return None
    cap = _CAP_HOLDER[0]
    if cap is None:
        return None
    fps = _CAP_FPS[0] or 30.0
    total = _CAP_FRAMES[0]

    idx = int(elapsed_s * fps)
    if total > 0:
        if loop:
            idx %= total
        else:
            if idx >= total:
                idx = total - 1
    else:
        # Unknown length — just cap on elapsed seconds.
        if elapsed_s > max_seconds_cap:
            elapsed_s = max_seconds_cap
        idx = max(0, int(elapsed_s * fps))

    if idx == _LAST_FRAME_IDX[0] and _LAST_FRAME_BGRA[0] is not None:
        return _LAST_FRAME_BGRA[0]

    try:
        # cv2.CAP_PROP_POS_FRAMES is the cheapest random-access; for purely
        # forward playback decoders it still seeks accurately for short clips.
        cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))  # type: ignore[union-attr]
        ok, frame = cap.read()
    except Exception:
        ok, frame = False, None
    if not ok or frame is None:
        # Rewind + retry once for codecs that bail at EOF when looping.
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0.0)  # type: ignore[union-attr]
            ok, frame = cap.read()
        except Exception:
            ok, frame = False, None
        if not ok or frame is None:
            return _LAST_FRAME_BGRA[0]

    bgra = _frame_to_bgra(frame)
    if bgra is not None:
        _LAST_FRAME_IDX[0] = idx
        _LAST_FRAME_BGRA[0] = bgra
    return bgra


def release_startup_cap() -> None:
    """Release the underlying capture (call when shutting down)."""
    cap = _CAP_HOLDER[0]
    if cap is not None:
        try:
            cap.release()  # type: ignore[attr-defined]
        except Exception:
            pass
    _CAP_HOLDER[0] = None
    _CAP_PATH[0] = None
    _CAP_FPS[0] = 0.0
    _CAP_FRAMES[0] = 0
    _LAST_FRAME_IDX[0] = -1
    _LAST_FRAME_BGRA[0] = None
