"""HDMI frame → OCR lines → OcrClues. Soft-fails if capture or Tesseract is missing.

Runs off the UI thread. At most one OCR pass at a time.
Pause checks: rising edge only, and not more than once per minute.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pigeon.ocr_clues import OcrClues, clues_agree_with_metadata, clues_from_lines

CAPTURE_INDEX = 0
PREFERRED_WIDTH = 1920
PREFERRED_HEIGHT = 1080
PAUSE_OCR_MIN_INTERVAL_S = 60.0
NO_METADATA_RETRY_S = 5.0
POSITION_STALL_S = 5.0
POSITION_ADVANCE_EPS = 0.2
OCR_TITLE_CONFIRM_HITS = 2
TITLE_MIN_HEIGHT = 40
TOP_RIGHT_X, TOP_RIGHT_Y, TOP_RIGHT_W, TOP_RIGHT_H = 0.52, 0.0, 0.48, 0.30

_HDMI_NAME_HINTS = (
    "blueavs",
    "hdmi",
    "capture",
    "usb video",
    "av to usb",
    "magewell",
    "elgato",
    "cam link",
    "video capture",
)
_SKIP_NAME_HINTS = (
    "facetime",
    "isight",
    "iphone",
    "ipad",
    "continuity",
    "studio display",
    "desk view",
    "macbook",
    "built-in",
)

OnClues = Callable[[OcrClues], None]


@dataclass
class OcrSchedule:
    was_paused: bool = False
    last_pause_ocr_mono: float = 0.0
    last_no_meta_ocr_mono: float = 0.0
    last_content_key: str | None = None
    last_confirm_key: str | None = None
    last_position: float | None = None
    last_position_change_mono: float = 0.0
    in_flight: bool = False


_schedule = OcrSchedule()


def reset_ocr_schedule() -> None:
    """Clear cadence / position memory (tests, HDMI off)."""
    global _schedule
    _schedule = OcrSchedule()


def _position_is_advancing(md: Mapping[str, Any], now: float) -> bool:
    """Track reported position; False when missing or unchanged past the stall window."""
    from pigeon.display_confidence import parse_position

    pos = parse_position(md)
    if pos is None:
        return False
    prev = _schedule.last_position
    if prev is None or abs(pos - prev) >= POSITION_ADVANCE_EPS:
        _schedule.last_position = pos
        _schedule.last_position_change_mono = now
        return True
    return (now - _schedule.last_position_change_mono) < POSITION_STALL_S
_lock = threading.Lock()
_cap = None
_cap_index: int | None = None
_last_capture_label = ""
_tesseract_ready = False
_av_devices_cache: list[tuple[int, str, str]] | None = None
_hdmi_present: bool | None = None
_hdmi_probe_mono: float = 0.0
_hdmi_probe_in_flight: bool = False
_HDMI_PROBE_TTL_S = 8.0


def _can_pass_to_tmdb(md: Mapping[str, Any]) -> bool:
    """True when we already have a title TMDb will accept."""
    try:
        from pigeon.tmdb_poster import is_degenerate_tmdb_query
    except ImportError:

        def is_degenerate_tmdb_query(q: str) -> bool:  # type: ignore[misc]
            return len(str(q or "").strip()) < 2

    from pigeon.ocr_clues import looks_like_ocr_junk

    for key in ("ocr_title", "query"):
        q = str(md.get(key) or "").strip()
        if q and not is_degenerate_tmdb_query(q) and not looks_like_ocr_junk(q):
            return True
    return False


def decide_ocr_reason(metadata: Mapping[str, Any] | None, now: float | None = None) -> str | None:
    """When should we look at HDMI?

    ``no_metadata`` — player title is missing; OCR is in charge.
    ``watch`` — playback undetected or position stalled; stay alert for the next card.
    ``confirm`` — content key changed and we already have a title.
    ``pause`` — rising pause edge (at most once a minute).
    """
    now = time.monotonic() if now is None else now
    md = metadata if isinstance(metadata, dict) else {}
    ds = str(md.get("device_state") or "").lower()
    paused = "paused" in ds or ds.endswith("pause")
    content_key = str(md.get("content_key") or "") or None
    tmdb_ready = _can_pass_to_tmdb(md)
    from pigeon.display_confidence import ocr_is_in_charge, playback_detected

    ocr_owns = ocr_is_in_charge(md, hdmi_present=hdmi_capture_available())
    advancing = _position_is_advancing(md, now)
    stay_alert = (not playback_detected(md)) or (not advancing)

    key_changed = content_key != _schedule.last_content_key
    if key_changed:
        _schedule.last_content_key = content_key

    rising_pause = paused and not _schedule.was_paused
    _schedule.was_paused = paused
    if rising_pause and (now - _schedule.last_pause_ocr_mono) >= PAUSE_OCR_MIN_INTERVAL_S:
        _schedule.last_pause_ocr_mono = now
        return "pause"

    if key_changed:
        if not tmdb_ready or ocr_owns:
            _schedule.last_no_meta_ocr_mono = now
            return "no_metadata"
        if content_key and content_key != _schedule.last_confirm_key:
            _schedule.last_confirm_key = content_key
            return "confirm"

    due = (now - _schedule.last_no_meta_ocr_mono) >= NO_METADATA_RETRY_S
    if not due:
        return None
    if ocr_owns or not tmdb_ready:
        _schedule.last_no_meta_ocr_mono = now
        return "no_metadata"
    if stay_alert:
        _schedule.last_no_meta_ocr_mono = now
        return "watch"
    return None


def request_ocr(reason: str, on_done: OnClues) -> bool:
    """Start one background OCR pass. Returns False if already busy or OCR is unavailable."""
    try:
        from pigeon.source_toggles import source_enabled

        if not source_enabled("hdmi"):
            return False
    except Exception:
        pass
    if not hdmi_capture_available():
        return False
    with _lock:
        if _schedule.in_flight:
            return False
        _schedule.in_flight = True
    thread = threading.Thread(
        target=_ocr_worker,
        args=(reason, on_done),
        name="hdmi-ocr",
        daemon=True,
    )
    thread.start()
    return True


def _ocr_worker(reason: str, on_done: OnClues) -> None:
    global _last_capture_label
    clues = OcrClues(reason=reason)
    try:
        frame = _grab_frame()
        if frame is None:
            _last_capture_label = "none"
            clues = OcrClues(reason=reason, extras=["capture_unavailable"])
        elif not _configure_tesseract():
            h, w = frame.shape[:2]
            name = _device_name(int(_cap_index)) if _cap_index is not None else ""
            _last_capture_label = f"{_cap_index} {name} {w}x{h}".strip()
            clues = OcrClues(
                reason=reason,
                extras=[_tesseract_error or "tesseract_unavailable"],
            )
        else:
            h, w = frame.shape[:2]
            name = _device_name(int(_cap_index)) if _cap_index is not None else ""
            _last_capture_label = f"{_cap_index} {name} {w}x{h}".strip()
            lines = _ocr_lines(frame)
            clues = clues_from_lines(lines, reason=reason)
    except Exception:
        clues = OcrClues(reason=reason, extras=["ocr_error"])
    finally:
        with _lock:
            _schedule.in_flight = False
    try:
        on_done(clues)
    except Exception:
        pass


def apply_clues_to_metadata(
    metadata: dict[str, Any],
    clues: OcrClues,
) -> dict[str, Any]:
    """Copy OCR clues onto the live metadata dict. Does not drop pyatv fields."""
    out = dict(metadata)
    prev_title = str(out.get("ocr_title") or "").strip()
    out.update(clues.as_metadata_fields())
    if not str(out.get("ocr_title") or "").strip() and prev_title:
        out["ocr_title"] = prev_title
    out["ocr_agrees"] = clues_agree_with_metadata(clues, metadata)
    out["ocr_at"] = time.time()
    extra0 = ""
    if isinstance(out.get("ocr_extras"), list) and out["ocr_extras"]:
        extra0 = str(out["ocr_extras"][0])
    if extra0 in ("capture_unavailable", "tesseract_unavailable", "ocr_error"):
        out["ocr_status"] = extra0
    elif str(out.get("ocr_title") or "").strip() or (
        isinstance(out.get("ocr_lines"), list) and out["ocr_lines"]
    ):
        out["ocr_status"] = "ok"
    else:
        out["ocr_status"] = "no_text"
    if _last_capture_label:
        out["ocr_capture"] = _last_capture_label
    return out


def _identity_is_placeholder(value: str) -> bool:
    from pigeon.display_confidence import is_placeholder_identity

    return is_placeholder_identity(value)


def ocr_session_anchor(metadata: Mapping[str, Any] | None) -> str:
    """pyatv-only identity used to decide whether last HDMI clues still apply."""
    md = metadata if isinstance(metadata, dict) else {}
    app = str(md.get("app_id") or md.get("app_name") or "").strip().casefold()
    query = str(md.get("query") or "").strip()
    if _identity_is_placeholder(query):
        query = ""
    return f"{app}|{query.casefold()}"


def apply_ocr_title_as_identity(metadata: dict[str, Any]) -> bool:
    """Fill or replace identity from OCR when the player did not supply a title.

    First good read fills an empty identity. A different title (next card, or a
    service that just lost pyatv metadata) must match twice before it takes over.
    Player-owned titles are left alone.
    """
    from pigeon.display_confidence import (
        OCR_IDENTITY,
        mark_identity,
        player_metadata_adequate,
    )

    guess = str(metadata.get("ocr_title") or "").strip()
    if not guess or _identity_is_placeholder(guess):
        return False
    if player_metadata_adequate(metadata):
        return False
    current = str(metadata.get("query") or "").strip()
    if _identity_is_placeholder(current):
        metadata["query"] = guess
        if _identity_is_placeholder(str(metadata.get("title") or "")):
            metadata["title"] = guess
        mark_identity(metadata, source="ocr", confidence=OCR_IDENTITY)
        metadata["ocr_pending_title"] = ""
        metadata["ocr_pending_hits"] = 0
        return True
    if current.casefold() == guess.casefold():
        mark_identity(metadata, source="ocr", confidence=OCR_IDENTITY)
        metadata["ocr_pending_title"] = ""
        metadata["ocr_pending_hits"] = 0
        return False
    pending = str(metadata.get("ocr_pending_title") or "").strip()
    hits = int(metadata.get("ocr_pending_hits") or 0)
    if pending.casefold() == guess.casefold():
        hits += 1
    else:
        pending = guess
        hits = 1
    metadata["ocr_pending_title"] = pending
    metadata["ocr_pending_hits"] = hits
    if hits < OCR_TITLE_CONFIRM_HITS:
        return False
    metadata["query"] = guess
    metadata["title"] = guess
    mark_identity(metadata, source="ocr", confidence=OCR_IDENTITY)
    metadata["ocr_pending_title"] = ""
    metadata["ocr_pending_hits"] = 0
    return True


_OCR_FIELD_KEYS = (
    "ocr_title",
    "ocr_lines",
    "ocr_season",
    "ocr_episode",
    "ocr_year",
    "ocr_runtime_min",
    "ocr_extras",
    "ocr_reason",
    "ocr_agrees",
    "ocr_at",
    "ocr_status",
    "ocr_capture",
    "ocr_pending_title",
    "ocr_pending_hits",
)


def copy_ocr_fields(src: Mapping[str, Any] | None, dest: dict[str, Any]) -> None:
    """Keep last OCR clues across a pyatv poll that does not re-run OCR."""
    if not isinstance(src, dict):
        return
    for key in _OCR_FIELD_KEYS:
        if key in src:
            dest[key] = src[key]


def clear_ocr_fields(dest: dict[str, Any]) -> None:
    """Drop HDMI OCR clues when that source is toggled off."""
    for key in _OCR_FIELD_KEYS:
        dest.pop(key, None)


def release_capture() -> None:
    """Free the HDMI capture device so another process can use it."""
    global _cap, _cap_index
    reset_ocr_schedule()
    with _lock:
        cap = _cap
        _cap = None
        _cap_index = None
    if cap is None:
        return
    try:
        cap.release()
    except Exception:
        pass


_tesseract_error = ""


def _configure_tesseract() -> bool:
    global _tesseract_ready, _tesseract_error
    if _tesseract_ready:
        return True
    try:
        import pytesseract
    except ImportError:
        _tesseract_error = "pytesseract_missing"
        return False
    brew_bin = "/opt/homebrew/bin"
    path_now = os.environ.get("PATH") or ""
    if brew_bin not in path_now.split(":"):
        os.environ["PATH"] = f"{brew_bin}:{path_now}" if path_now else brew_bin
    if not os.environ.get("TESSDATA_PREFIX"):
        for tessdata in (
            "/opt/homebrew/share/tessdata",
            "/usr/local/share/tessdata",
            "/usr/share/tesseract-ocr/5/tessdata",
            "/usr/share/tesseract-ocr/4.00/tessdata",
        ):
            if os.path.isfile(os.path.join(tessdata, "eng.traineddata")):
                os.environ["TESSDATA_PREFIX"] = tessdata
                break
    for path in (
        shutil.which("tesseract"),
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    ):
        if path and os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            try:
                pytesseract.get_tesseract_version()
            except Exception:
                continue
            _tesseract_ready = True
            _tesseract_error = ""
            return True
    _tesseract_error = "tesseract_bin_missing"
    return False


def _is_skip_camera(name: str, dtype: str = "") -> bool:
    blob = f"{name} {dtype}".lower()
    return any(skip in blob for skip in _SKIP_NAME_HINTS)


def _is_hdmi_camera(name: str, dtype: str = "") -> bool:
    if _is_skip_camera(name, dtype):
        return False
    low = name.lower()
    return any(hint in low for hint in _HDMI_NAME_HINTS)


def note_hdmi_present(present: bool) -> None:
    """Remember whether the HDMI dongle is plugged in (settings LED)."""
    global _hdmi_present
    _hdmi_present = bool(present)


def hdmi_capture_available() -> bool:
    """Non-blocking: True when a capture device is open or last seen."""
    global _hdmi_present
    if _cap is not None:
        try:
            if _cap.isOpened():
                _hdmi_present = True
                return True
        except Exception:
            pass
    if _hdmi_present is not None:
        return bool(_hdmi_present)
    cached = _av_devices_cache
    if cached is not None:
        return any(_is_hdmi_camera(name, dtype) for _i, name, dtype in cached)
    return False


def probe_hdmi_presence(*, force: bool = False) -> bool:
    """Return last known presence; refresh the device list in the background."""
    global _hdmi_probe_in_flight, _hdmi_probe_mono
    now = time.monotonic()
    stale = (
        force
        or _hdmi_probe_mono <= 0.0
        or (now - _hdmi_probe_mono) >= _HDMI_PROBE_TTL_S
    )
    if stale and not _hdmi_probe_in_flight:
        _hdmi_probe_in_flight = True
        threading.Thread(
            target=_hdmi_probe_worker, name="hdmi-probe", daemon=True
        ).start()
    return hdmi_capture_available()


def _hdmi_probe_worker() -> None:
    global _hdmi_present, _hdmi_probe_in_flight, _hdmi_probe_mono, _av_devices_cache
    try:
        _av_devices_cache = None
        present = False
        if sys.platform == "darwin":
            named = _avfoundation_devices()
            present = any(_is_hdmi_camera(name, dtype) for _i, name, dtype in named)
        elif sys.platform.startswith("linux"):
            import glob

            present = bool(glob.glob("/dev/video*"))
        if not present and _cap is not None:
            try:
                present = bool(_cap.isOpened())
            except Exception:
                present = False
        _hdmi_present = present
        _hdmi_probe_mono = time.monotonic()
    finally:
        _hdmi_probe_in_flight = False


def _avfoundation_devices() -> list[tuple[int, str, str]]:
    """OpenCV AVFoundation indexes with names. Cached; Swift is slow to start."""
    global _av_devices_cache
    if _av_devices_cache is not None:
        return _av_devices_cache
    if sys.platform != "darwin":
        _av_devices_cache = []
        return _av_devices_cache
    # Same enumeration OpenCV uses: video + muxed, sorted by uniqueID.
    script = (
        "import AVFoundation\n"
        "import Foundation\n"
        "let video = AVCaptureDevice.devices(for: .video)\n"
        "let muxed = AVCaptureDevice.devices(for: .muxed)\n"
        "let all = (video + muxed).sorted { $0.uniqueID < $1.uniqueID }\n"
        "for (i, d) in all.enumerated() {\n"
        '    print("\\(i)\\t\\(d.localizedName)\\t\\(d.deviceType.rawValue)")\n'
        "}\n"
    )
    path = ""
    try:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".swift", delete=False) as handle:
            handle.write(script)
            path = handle.name
        result = subprocess.run(
            ["swift", path],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _av_devices_cache = []
        return _av_devices_cache
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    out: list[tuple[int, str, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        idx = int(parts[0].strip())
        name = parts[1].strip()
        dtype = parts[2].strip() if len(parts) > 2 else ""
        if name:
            out.append((idx, name, dtype))
    _av_devices_cache = out
    return _av_devices_cache


def _device_name(index: int) -> str:
    for idx, name, _dtype in _avfoundation_devices():
        if idx == index:
            return name
    return ""


def _candidate_capture_indices() -> list[int]:
    env = str(os.environ.get("PIGEON_HDMI_CAPTURE_INDEX") or "").strip()
    if env.isdigit():
        return [int(env)]
    named = _avfoundation_devices()
    hdmi = [i for i, name, dtype in named if _is_hdmi_camera(name, dtype)]
    if hdmi:
        return hdmi
    other = [i for i, name, dtype in named if not _is_skip_camera(name, dtype)]
    if other:
        return other
    if named:
        return []
    # Do not scan 0..3 on macOS — Continuity Camera often sits at index 1.
    if sys.platform == "darwin":
        return []
    out = [CAPTURE_INDEX]
    for extra in (1, 2, 3):
        if extra not in out:
            out.append(extra)
    return out


def _opencv_backend():
    import cv2

    if sys.platform == "darwin" and hasattr(cv2, "CAP_AVFOUNDATION"):
        return cv2.CAP_AVFOUNDATION
    if sys.platform.startswith("linux") and hasattr(cv2, "CAP_V4L2"):
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


def _open_capture_at(index: int):
    import cv2

    cap = cv2.VideoCapture(index, _opencv_backend())
    if cap is None or not cap.isOpened():
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        return None
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(PREFERRED_WIDTH))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(PREFERRED_HEIGHT))
    frame = None
    for _ in range(8):
        ok, frame = cap.read()
        if ok and frame is not None and getattr(frame, "size", 0):
            return cap
    try:
        cap.release()
    except Exception:
        pass
    return None


def _grab_frame():
    global _cap, _cap_index
    try:
        import cv2  # noqa: F401
    except ImportError:
        return None
    if _cap is None or not _cap.isOpened():
        _cap = None
        _cap_index = None
        env = str(os.environ.get("PIGEON_HDMI_CAPTURE_INDEX") or "").strip()
        if env.isdigit():
            opened = _open_capture_at(int(env))
            if opened is not None:
                _cap = opened
                _cap_index = int(env)
        else:
            # Prefer the named HDMI dongle (blueAVS). Never open iPhone / FaceTime
            # when a capture card is present — iPhone is also "external" and 1080p.
            for index in _candidate_capture_indices():
                opened = _open_capture_at(index)
                if opened is None:
                    continue
                _cap = opened
                _cap_index = index
                break
        if _cap is None:
            note_hdmi_present(False)
            return None
    ok, frame = _cap.read()
    if not ok or frame is None or not getattr(frame, "size", 0):
        note_hdmi_present(False)
        try:
            _cap.release()
        except Exception:
            pass
        _cap = None
        _cap_index = None
        _av_devices_cache = None
        return None
    note_hdmi_present(True)
    return frame


def _ocr_lines(bgr_frame) -> list[str]:
    if not _configure_tesseract():
        return []
    import cv2

    hits = _hits_from_image(_gray_variants(bgr_frame))
    hits.extend(_hits_top_right(bgr_frame))
    hits.extend(_hits_left_title(bgr_frame))
    hits = _dedupe_hits(hits)
    grouped = _group_lines(hits)
    return [h[0] for h in grouped if h[0]]


def _gray_variants(bgr_frame) -> list:
    import cv2

    gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    boosted = clahe.apply(gray)
    return [boosted, cv2.bitwise_not(boosted), gray]


def _dedupe_hits(hits: list[tuple]) -> list[tuple]:
    """Keep the highest-confidence copy when several passes see the same word."""
    best: dict[tuple, tuple] = {}
    for hit in hits:
        text, conf, left, top, width, height = hit
        key = (text.lower(), int(left) // 24, int(top) // 16)
        prior = best.get(key)
        if prior is None or float(conf) > float(prior[1]):
            best[key] = hit
    return list(best.values())


def _hits_from_image(images, min_conf: float = 30.0, config: str = "--psm 11") -> list[tuple]:
    import pytesseract

    out: list[tuple] = []
    for image in images:
        data = pytesseract.image_to_data(
            image, lang="eng", config=config, output_type=pytesseract.Output.DICT
        )
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text or not any(ch.isalnum() for ch in text):
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                continue
            if conf < min_conf:
                continue
            left, top = int(data["left"][i]), int(data["top"][i])
            width, height = int(data["width"][i]), int(data["height"][i])
            if width < 6 or height < 8:
                continue
            out.append((text, conf, left, top, width, height))
    return out


def _hits_top_right(bgr_frame) -> list[tuple]:
    import cv2

    h, w = bgr_frame.shape[:2]
    x0 = int(w * TOP_RIGHT_X)
    y0 = int(h * TOP_RIGHT_Y)
    x1 = min(w, int(w * (TOP_RIGHT_X + TOP_RIGHT_W)))
    y1 = min(h, int(h * (TOP_RIGHT_Y + TOP_RIGHT_H)))
    crop = bgr_frame[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, bright = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY)
    paper = cv2.bitwise_not(bright)
    scale = 3.0
    big = cv2.resize(paper, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    raw = _hits_from_image([big], min_conf=25.0, config="--psm 7")
    shifted: list[tuple] = []
    for text, conf, left, top, width, height in raw:
        shifted.append(
            (
                text,
                conf,
                x0 + int(left / scale),
                y0 + int(top / scale),
                max(1, int(width / scale)),
                max(1, int(height / scale)),
            )
        )
    return shifted


def _hits_left_title(bgr_frame) -> list[tuple]:
    """Disney+ / Apple TV title cards put the name on the left, not top-right."""
    import cv2

    h, w = bgr_frame.shape[:2]
    x0, y0 = 0, int(h * 0.12)
    x1, y1 = int(w * 0.58), int(h * 0.62)
    crop = bgr_frame[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    boosted = clahe.apply(gray)
    _, bright = cv2.threshold(boosted, 160, 255, cv2.THRESH_BINARY)
    paper = cv2.bitwise_not(bright)
    scale = 2.0
    big = cv2.resize(paper, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    raw = _hits_from_image([big], min_conf=25.0, config="--psm 11")
    shifted: list[tuple] = []
    for text, conf, left, top, width, height in raw:
        shifted.append(
            (
                text,
                conf,
                x0 + int(left / scale),
                y0 + int(top / scale),
                max(1, int(width / scale)),
                max(1, int(height / scale)),
            )
        )
    return shifted


def _group_lines(hits: list[tuple]) -> list[tuple]:
    if not hits:
        return []
    rows: list[list[tuple]] = []
    for hit in sorted(hits, key=lambda h: (h[3], h[2])):
        placed = False
        for row in rows:
            if _same_row(row, hit):
                row.append(hit)
                placed = True
                break
        if not placed:
            rows.append([hit])
    grouped: list[tuple] = []
    for row in rows:
        row.sort(key=lambda h: h[2])
        clusters: list[list[tuple]] = [[row[0]]]
        for hit in row[1:]:
            last = clusters[-1][-1]
            gap = hit[2] - (last[2] + last[4])
            typical_h = max(last[5], hit[5])
            limit = max(36, typical_h * 1.2) if typical_h >= TITLE_MIN_HEIGHT else max(180, typical_h * 8)
            if gap <= limit:
                clusters[-1].append(hit)
            else:
                clusters.append([hit])
        for cluster in clusters:
            text = " ".join(h[0] for h in cluster)
            left = min(h[2] for h in cluster)
            top = min(h[3] for h in cluster)
            right = max(h[2] + h[4] for h in cluster)
            bottom = max(h[3] + h[5] for h in cluster)
            grouped.append((text, left, top, right - left, bottom - top))
    grouped.sort(key=lambda h: (h[2], h[1]))
    return grouped


def _same_row(row: list[tuple], hit: tuple) -> bool:
    line_c = sum(h[3] + h[5] / 2.0 for h in row) / len(row)
    hit_c = hit[3] + hit[5] / 2.0
    taller = max(max(h[5] for h in row), hit[5])
    if abs(hit_c - line_c) > taller * 0.55:
        return False
    line_h = max(h[5] for h in row)
    smaller, big = sorted((line_h, hit[5]))
    if smaller < 1:
        return False
    if big < TITLE_MIN_HEIGHT:
        return True
    return big / smaller <= 2.2
