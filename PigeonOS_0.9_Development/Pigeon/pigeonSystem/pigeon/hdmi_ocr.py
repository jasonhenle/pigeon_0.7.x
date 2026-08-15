"""HDMI frame → OCR lines → OcrClues. Soft-fails if capture or Tesseract is missing.

Runs off the UI thread. At most one OCR pass at a time.
Pause checks: rising edge only, and not more than once per minute.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pigeon.ocr_clues import OcrClues, clues_agree_with_metadata, clues_from_lines

CAPTURE_INDEX = 0
PREFERRED_WIDTH = 1920
PREFERRED_HEIGHT = 1080
PAUSE_OCR_MIN_INTERVAL_S = 60.0
TITLE_MIN_HEIGHT = 40
TOP_RIGHT_X, TOP_RIGHT_Y, TOP_RIGHT_W, TOP_RIGHT_H = 0.52, 0.0, 0.48, 0.30

OnClues = Callable[[OcrClues], None]


@dataclass
class OcrSchedule:
    was_paused: bool = False
    last_pause_ocr_mono: float = 0.0
    last_content_key: str | None = None
    last_confirm_key: str | None = None
    in_flight: bool = False


_schedule = OcrSchedule()
_lock = threading.Lock()
_cap = None
_tesseract_ready = False


def decide_ocr_reason(metadata: Mapping[str, Any] | None, now: float | None = None) -> str | None:
    """When should we look at HDMI? ``no_metadata`` | ``confirm`` | ``pause`` | None."""
    now = time.monotonic() if now is None else now
    md = metadata if isinstance(metadata, dict) else {}
    ds = str(md.get("device_state") or "").lower()
    paused = "paused" in ds or ds.endswith("pause")
    query = str(md.get("query") or "").strip()
    idle = "idle" in ds or "stopped" in ds
    content_key = str(md.get("content_key") or "") or None
    has_query = bool(query) and not idle

    rising_pause = paused and not _schedule.was_paused
    _schedule.was_paused = paused
    if rising_pause and (now - _schedule.last_pause_ocr_mono) >= PAUSE_OCR_MIN_INTERVAL_S:
        _schedule.last_pause_ocr_mono = now
        return "pause"

    if content_key != _schedule.last_content_key:
        _schedule.last_content_key = content_key
        if not has_query:
            return "no_metadata"
        if content_key and content_key != _schedule.last_confirm_key:
            _schedule.last_confirm_key = content_key
            return "confirm"
    # Playing or paused with no Apple TV/Roku title: keep reading HDMI until OCR
    # has a title. Pause used to be rising-edge only, so a still pause screen
    # never retried and never reached TMDb.
    active = paused or "playing" in ds
    if (
        not has_query
        and active
        and not idle
        and not str(md.get("ocr_title") or "").strip()
    ):
        return "no_metadata"
    return None


def request_ocr(reason: str, on_done: OnClues) -> bool:
    """Start one background OCR pass. Returns False if already busy or OCR is unavailable."""
    try:
        from pigeon.source_toggles import source_enabled

        if not source_enabled("hdmi"):
            return False
    except Exception:
        pass
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
    clues = OcrClues(reason=reason)
    try:
        frame = _grab_frame()
        if frame is not None:
            lines = _ocr_lines(frame)
            clues = clues_from_lines(lines, reason=reason)
    except Exception:
        clues = OcrClues(reason=reason)
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
    out.update(clues.as_metadata_fields())
    out["ocr_agrees"] = clues_agree_with_metadata(clues, metadata)
    out["ocr_at"] = time.time()
    return out


def apply_ocr_title_as_identity(metadata: dict[str, Any]) -> bool:
    """Fill empty query/title from OCR so now-playing and TMDb have an identity."""
    guess = str(metadata.get("ocr_title") or "").strip()
    if not guess:
        return False
    changed = False
    if not str(metadata.get("query") or "").strip():
        metadata["query"] = guess
        changed = True
    if not str(metadata.get("title") or "").strip():
        metadata["title"] = guess
        changed = True
    return changed


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
)


def copy_ocr_fields(src: Mapping[str, Any] | None, dest: dict[str, Any]) -> None:
    """Keep last OCR clues across a pyatv poll that does not re-run OCR."""
    if not isinstance(src, dict):
        return
    for key in _OCR_FIELD_KEYS:
        if key in src and key not in dest:
            dest[key] = src[key]


def clear_ocr_fields(dest: dict[str, Any]) -> None:
    """Drop HDMI OCR clues when that source is toggled off."""
    for key in _OCR_FIELD_KEYS:
        dest.pop(key, None)


def release_capture() -> None:
    """Free the HDMI capture device so another process can use it."""
    global _cap
    with _lock:
        cap = _cap
        _cap = None
    if cap is None:
        return
    try:
        cap.release()
    except Exception:
        pass


def _configure_tesseract() -> bool:
    global _tesseract_ready
    if _tesseract_ready:
        return True
    try:
        import pytesseract
    except ImportError:
        return False
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
            return True
    return False


def _grab_frame():
    global _cap
    try:
        import cv2
    except ImportError:
        return None
    backend = cv2.CAP_AVFOUNDATION if hasattr(cv2, "CAP_AVFOUNDATION") else cv2.CAP_ANY
    if _cap is None or not _cap.isOpened():
        _cap = cv2.VideoCapture(CAPTURE_INDEX, backend)
        if _cap is None or not _cap.isOpened():
            return None
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        _cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        _cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(PREFERRED_WIDTH))
        _cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(PREFERRED_HEIGHT))
    ok, frame = _cap.read()
    if not ok or frame is None or not getattr(frame, "size", 0):
        return None
    return frame


def _ocr_lines(bgr_frame) -> list[str]:
    if not _configure_tesseract():
        return []
    import cv2

    hits = _hits_from_image(_gray_variants(bgr_frame))
    hits.extend(_hits_top_right(bgr_frame))
    grouped = _group_lines(hits)
    return [h[0] for h in grouped if h[0]]


def _gray_variants(bgr_frame) -> list:
    import cv2

    gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
    return [gray, cv2.bitwise_not(gray)]


def _hits_from_image(images, min_conf: float = 45.0, config: str = "--psm 11") -> list[tuple]:
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
