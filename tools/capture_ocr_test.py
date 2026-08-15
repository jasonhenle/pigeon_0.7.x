#!/usr/bin/env python3
"""Standalone HDMI capture + OCR diagnostic (not part of the Pigeon app).

What this program does
----------------------
1. Finds USB video capture devices (HDMI capture dongles, webcams, etc.).
2. Opens a live video window from the chosen device.
3. About once per second, copies one full-resolution frame.
4. Runs Tesseract OCR on that frame in a background thread.
5. Prints what OCR saw, and draws boxes/text on the preview.

Why Tesseract (not EasyOCR / Apple Vision)
------------------------------------------
Tesseract runs locally on macOS and on a Raspberry Pi, needs no GPU, and
returns text + confidence + bounding boxes. EasyOCR is often more accurate
on stylized UI text, but it pulls in a large neural-net stack and is slower
on a Pi. Apple Vision is macOS-only. Start here; switch later if needed.

Install (once)
--------------
    # OCR engine (macOS)
    brew install tesseract

    # Python packages (use the same Python you will run this with)
    python3 -m pip install opencv-python numpy pillow pytesseract

Run
---
    python3 tools/capture_ocr_test.py
    python3 tools/capture_ocr_test.py --list
    python3 tools/capture_ocr_test.py --device 1
    python3 tools/capture_ocr_test.py --image ocr_test_frames/capture_....png

Keys in the video window
------------------------
    S  save the current raw frame into ocr_test_frames/
    Q  quit  (Esc also quits)
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Easy-to-change defaults (also available as command-line flags)
# ---------------------------------------------------------------------------
DEFAULT_DEVICE_INDEX = 0
OCR_INTERVAL_SEC = 1.0
PREFERRED_WIDTH = 1920
PREFERRED_HEIGHT = 1080
PREFERRED_FPS = 30
# Ignore Tesseract hits below this confidence (0–100). 60 hides most poster
# texture that contrast boost turns into fake words. Use --min-conf 40 to
# see more, including junk.
DEFAULT_MIN_CONF = 60
# Preview-only scale. OCR always uses the original captured pixels.
PREVIEW_MAX_WIDTH = 1280
PREVIEW_MAX_HEIGHT = 720
# Local contrast for faded Apple TV type. Higher = stronger (2–4 is typical).
CLAHE_CLIP = 3.0
CLAHE_TILE = 8
# Title-sized boxes (px tall). These often lose the last letter (SILO → SIL).
TITLE_MIN_HEIGHT = 40
# Netflix puts the title in the top-right. Fractions of the full frame.
TOP_RIGHT_X = 0.52
TOP_RIGHT_Y = 0.0
TOP_RIGHT_W = 0.48
TOP_RIGHT_H = 0.30

SCRIPT_DIR = Path(__file__).resolve().parent
SAVE_DIR = SCRIPT_DIR / "ocr_test_frames"
WINDOW_NAME = "Pigeon OCR Test"


# ---------------------------------------------------------------------------
# Small data type for one OCR hit
# ---------------------------------------------------------------------------
@dataclass
class OcrHit:
    """One piece of text Tesseract found in the frame.

    After grouping, ``text`` may be a whole line (several words joined).
    """

    text: str
    confidence: float  # 0–100
    left: int
    top: int
    width: int
    height: int


def looks_like_icon(hit: OcrHit) -> bool:
    """True for Apple TV chrome that is an icon, not a word.

    Tesseract often reads the Home glyph, Plus button, and play marks as
    ``@``, ``+``, ``®``, ``»>``. Those have no letter or digit. Keep
    things like ``(R)`` and ``2`` because they do.
    """
    return not any(ch.isalnum() for ch in hit.text)


def looks_like_noise(hit: OcrHit) -> bool:
    """True for leftover specks invert/Tesseract sometimes emit (``s`` at 2x2)."""
    if looks_like_icon(hit):
        return True
    if hit.width < 6 or hit.height < 8:
        return True
    if hit.width * hit.height < 48:
        return True
    if len(hit.text) <= 2 and hit.width < 16 and hit.height < 14:
        return True
    return False


def _vertical_center(hit: OcrHit) -> float:
    return hit.top + hit.height / 2.0


def _similar_height(line: list[OcrHit], hit: OcrHit) -> bool:
    """Keep a large title off the genre row. Body words may differ a bit."""
    line_h = max(h.height for h in line)
    smaller, taller = sorted((line_h, hit.height))
    if smaller < 1:
        return False
    if taller < 40:
        return True
    return taller / smaller <= 2.2


def _close_enough_horizontally(line: list[OcrHit], hit: OcrHit) -> bool:
    """Do not join left-side copy with right-side cast across the full screen."""
    rightmost = max(h.left + h.width for h in line)
    leftmost = min(h.left for h in line)
    if hit.left > rightmost:
        gap = hit.left - rightmost
    elif hit.left + hit.width < leftmost:
        gap = leftmost - (hit.left + hit.width)
    else:
        return True
    typical_h = max(max(h.height for h in line), hit.height)
    # Tall titles must not swallow poster text to their right.
    if typical_h >= TITLE_MIN_HEIGHT:
        return gap <= max(36, typical_h * 1.2)
    # Body lines may skip a faded middle word.
    return gap <= max(180, typical_h * 8)


def _vertically_same_line(line: list[OcrHit], hit: OcrHit) -> bool:
    """Same row if centers align. Height is checked separately."""
    line_center = sum(_vertical_center(h) for h in line) / len(line)
    tol = max(max(h.height for h in line), hit.height) * 0.55
    return abs(_vertical_center(hit) - line_center) <= tol


def _join_line_text(words: list[str]) -> str:
    """Join words with spaces, but glue hyphenated pieces: mind- + bending."""
    parts: list[str] = []
    for word in words:
        if parts and parts[-1].endswith("-"):
            parts[-1] = parts[-1] + word
        else:
            parts.append(word)
    return " ".join(parts)


def group_hits_into_lines(hits: list[OcrHit]) -> list[OcrHit]:
    """Turn word boxes into one box per text line.

    Why: Tesseract returns each word separately. For Pigeon we want
    ``Continue Watching`` and the synopsis as lines, not 12 tiny hits.
    """
    if not hits:
        return []

    # Pass 1: cluster by row + similar type size (keeps LOOPER off the genre line).
    rows: list[list[OcrHit]] = []
    for hit in sorted(hits, key=lambda h: (h.top, h.left)):
        placed = False
        for row in rows:
            if _vertically_same_line(row, hit) and _similar_height(row, hit):
                row.append(hit)
                placed = True
                break
        if not placed:
            rows.append([hit])

    # Pass 2: split a row if words jump across a wide gap (left copy vs right cast).
    grouped: list[OcrHit] = []
    for row in rows:
        row.sort(key=lambda h: h.left)
        clusters: list[list[OcrHit]] = [[row[0]]]
        for hit in row[1:]:
            if _close_enough_horizontally(clusters[-1], hit):
                clusters[-1].append(hit)
            else:
                clusters.append([hit])
        for cluster in clusters:
            left = min(h.left for h in cluster)
            top = min(h.top for h in cluster)
            right = max(h.left + h.width for h in cluster)
            bottom = max(h.top + h.height for h in cluster)
            weights = [max(len(h.text), 1) for h in cluster]
            conf = sum(h.confidence * w for h, w in zip(cluster, weights)) / sum(weights)
            grouped.append(
                OcrHit(
                    text=_join_line_text([h.text for h in cluster]),
                    confidence=conf,
                    left=left,
                    top=top,
                    width=right - left,
                    height=bottom - top,
                )
            )
    grouped.sort(key=lambda h: (h.top, h.left))
    return grouped


# ---------------------------------------------------------------------------
# Capture: find devices and open the HDMI dongle
# ---------------------------------------------------------------------------
def opencv_backend() -> int:
    """Pick the native camera API for this OS. Imported lazily so --help works
    even if OpenCV is missing."""
    import cv2

    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


def macos_camera_names() -> list[str]:
    """Read camera / capture-card names from macOS. Does not open the devices."""
    if sys.platform != "darwin":
        return []
    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith("Model ID") and ":" in stripped:
            # system_profiler lists each camera as "Name:" at indent level.
            indent = len(line) - len(line.lstrip(" "))
            if indent == 4:
                names.append(stripped[:-1])
    return names


def probe_capture_indices(max_index: int = 6) -> list[tuple[int, int, int]]:
    """Try OpenCV indexes and report which ones actually open.

    Returns a list of (index, width, height). Opening a camera briefly may
    make the Mac camera light blink; that is expected for this diagnostic.
    """
    import cv2

    backend = opencv_backend()
    found: list[tuple[int, int, int]] = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index, backend)
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            continue
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        found.append((index, width, height))
        cap.release()
    return found


def print_device_list() -> None:
    print("Video capture devices")
    print("---------------------")
    names = macos_camera_names()
    if names:
        print("macOS camera names (order often matches OpenCV indexes):")
        for i, name in enumerate(names):
            print(f"  {i}: {name}")
        print()
    print("OpenCV indexes that opened:")
    found = probe_capture_indices()
    if not found:
        print("  (none)")
        print()
        print("If a USB HDMI capture is plugged in, try --device 1 (index 0 is")
        print("often the built-in webcam). On macOS also check:")
        print("  System Settings → Privacy & Security → Camera")
        return
    for index, width, height in found:
        print(f"  index {index}: {width}x{height}")
    print()
    print("On a Mac, index 0 is usually the FaceTime camera.")
    print("A USB HDMI capture dongle is often index 1.")
    print("Re-run with:  python3 tools/capture_ocr_test.py --device N")


def open_capture(index: int, width: int, height: int, fps: int):
    """Open a capture device and ask for a high resolution.

    Many USB HDMI dongles stay at 640x480 unless we request MJPG + 1080p.
    We ask; the device may still give us something smaller. We print whatever
    it actually granted. The frame is never resized before OCR.
    """
    import cv2

    backend = opencv_backend()
    cap = cv2.VideoCapture(index, backend)
    if cap is None or not cap.isOpened():
        if cap is not None:
            cap.release()
        return None, (
            f"Could not open capture device index {index}.\n"
            f"Backend: {backend_name(backend)}\n"
            "\n"
            "Common fixes:\n"
            "  1. Plug in the HDMI USB capture device.\n"
            "  2. Try another index:  python3 tools/capture_ocr_test.py --device 1\n"
            "  3. List devices:       python3 tools/capture_ocr_test.py --list\n"
            "  4. Quit Zoom / FaceTime / Photo Booth if they already own the camera.\n"
            "  5. On macOS: System Settings → Privacy & Security → Camera\n"
            "     and allow Terminal (or your Python app)."
        )

    # MJPG is the usual way USB capture cards send 1080p over USB 2.
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(height))
    cap.set(cv2.CAP_PROP_FPS, float(fps))

    # HDMI dongles often need a few frames before they sync.
    ok = False
    frame = None
    for _ in range(8):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size:
            break
        time.sleep(0.05)

    if not ok or frame is None or not frame.size:
        cap.release()
        return None, (
            f"Opened device index {index}, but could not read a video frame.\n"
            "The HDMI source may be off, or the capture card may need a different index."
        )

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or frame.shape[1])
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or frame.shape[0])
    actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    print(f"Opened capture index {index}  ({backend_name(backend)})")
    print(f"Requested: {width}x{height} @ {fps} fps")
    print(f"Granted:   {actual_w}x{actual_h} @ {actual_fps:.2f} fps")
    print(f"First frame pixels: {frame.shape[1]}x{frame.shape[0]}")
    if actual_w < 1280 or actual_h < 720:
        print(
            "Note: capture is below 1280x720. Small on-screen text will be harder "
            "for OCR. Try --width 1920 --height 1080, or another --device index."
        )
    print()
    return cap, None


def backend_name(backend: int) -> str:
    import cv2

    if backend == cv2.CAP_AVFOUNDATION:
        return "AVFOUNDATION"
    if backend in (cv2.CAP_V4L, cv2.CAP_V4L2):
        return "V4L2"
    return str(backend)


# ---------------------------------------------------------------------------
# OCR: Tesseract on one full frame
# ---------------------------------------------------------------------------
def configure_tesseract() -> None:
    """Point pytesseract at the tesseract binary, with a clear error if missing."""
    import pytesseract

    candidates = [
        shutil.which("tesseract"),
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            try:
                version = pytesseract.get_tesseract_version()
            except pytesseract.TesseractNotFoundError:
                continue
            print(f"Tesseract: {path}  (version {version})")
            return

    raise SystemExit(
        "Tesseract is not installed (or not on PATH).\n"
        "\n"
        "macOS:          brew install tesseract\n"
        "Raspberry Pi:   sudo apt install tesseract-ocr\n"
        "Python wrapper: python3 -m pip install pytesseract"
    )


# Sparse text: Apple TV is a UI, not a book page. Default page-split
# often stops a line early when the poster sits to the right.
TESSERACT_CONFIG = "--psm 11"


def boost_contrast(gray):
    """Brighten faded letters without blowing out the already-white title.

    CLAHE is a local contrast filter: it looks at small tiles of the image
    and stretches gray-on-black toward white-on-black. That is aimed at the
    right end of an Apple TV synopsis, which fades into the poster.
    """
    import cv2

    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(CLAHE_TILE, CLAHE_TILE))
    return clahe.apply(gray)


def prepare_gray_frames(bgr_frame, invert: bool, contrast: bool) -> list:
    """Grayscale copies for Tesseract. Preview still shows the real HDMI frame.

    Contrast first (faded synopsis). Then optional invert (bright titles).
    """
    import cv2

    gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
    if contrast:
        gray = boost_contrast(gray)
    frames = [gray]
    if invert:
        frames.append(cv2.bitwise_not(gray))
    return frames


def _hits_from_tesseract(ocr_image, min_conf: float, config: str = TESSERACT_CONFIG) -> list[OcrHit]:
    import pytesseract

    data = pytesseract.image_to_data(
        ocr_image,
        lang="eng",
        config=config,
        output_type=pytesseract.Output.DICT,
    )
    hits: list[OcrHit] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < 0 or conf < min_conf:
            continue
        hit = OcrHit(
            text=text,
            confidence=conf,
            left=int(data["left"][i]),
            top=int(data["top"][i]),
            width=int(data["width"][i]),
            height=int(data["height"][i]),
        )
        if looks_like_noise(hit):
            continue
        hits.append(hit)
    return hits


def _dedupe_hits(hits: list[OcrHit]) -> list[OcrHit]:
    """Keep the higher-confidence copy when both passes see the same word."""
    best: dict[tuple, OcrHit] = {}
    for hit in hits:
        key = (hit.text.lower(), hit.left // 24, hit.top // 16)
        prior = best.get(key)
        if prior is None or hit.confidence > prior.confidence:
            best[key] = hit
    return list(best.values())


def _shift_hits(hits: list[OcrHit], dx: int, dy: int) -> list[OcrHit]:
    """Move crop-local boxes back onto the full 1920x1080 frame."""
    return [
        OcrHit(h.text, h.confidence, h.left + dx, h.top + dy, h.width, h.height)
        for h in hits
    ]


def top_right_rect(frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    x0 = int(frame_w * TOP_RIGHT_X)
    y0 = int(frame_h * TOP_RIGHT_Y)
    x1 = min(frame_w, int(frame_w * (TOP_RIGHT_X + TOP_RIGHT_W)))
    y1 = min(frame_h, int(frame_h * (TOP_RIGHT_Y + TOP_RIGHT_H)))
    return x0, y0, x1, y1


def ocr_top_right(
    bgr_frame, min_conf: float, invert: bool, contrast: bool
) -> list[OcrHit]:
    """Second pass: only the top-right corner (Netflix title placement).

    Netflix titles are small, so we enlarge this crop 2x and allow a lower
    confidence than the full-frame pass.
    """
    import cv2

    height, width = bgr_frame.shape[:2]
    x0, y0, x1, y1 = top_right_rect(width, height)
    crop = bgr_frame[y0:y1, x0:x1]
    if crop.size == 0:
        return []
    scale = 3.0
    region_min = 20
    hits: list[OcrHit] = []

    def _add_from(image, interp) -> None:
        big = cv2.resize(image, None, fx=scale, fy=scale, interpolation=interp)
        for config in ("--psm 7", "--psm 11"):
            for hit in _hits_from_tesseract(big, region_min, config=config):
                hits.append(
                    OcrHit(
                        hit.text,
                        hit.confidence,
                        int(hit.left / scale),
                        int(hit.top / scale),
                        max(1, int(hit.width / scale)),
                        max(1, int(hit.height / scale)),
                    )
                )

    for image in prepare_gray_frames(crop, invert, contrast):
        _add_from(image, cv2.INTER_CUBIC)

    # Netflix HUD is small white type. Keep only bright pixels, then make
    # dark-on-light for Tesseract.
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, bright = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY)
    _add_from(cv2.bitwise_not(bright), cv2.INTER_NEAREST)

    shifted = _shift_hits(hits, x0, y0)
    if shifted:
        print("TOP-RIGHT RAW:", ", ".join(f"{h.text} ({h.confidence:.0f}%)" for h in shifted[:12]))
    return shifted


def run_ocr(
    bgr_frame,
    min_conf: float,
    group_lines: bool = True,
    invert: bool = True,
    contrast: bool = True,
) -> tuple[list[OcrHit], float]:
    """OCR the entire frame, plus a Netflix-style top-right crop."""
    started = time.perf_counter()
    hits: list[OcrHit] = []
    for image in prepare_gray_frames(bgr_frame, invert, contrast):
        hits.extend(_hits_from_tesseract(image, min_conf))
    hits.extend(ocr_top_right(bgr_frame, min_conf, invert, contrast))
    hits = _dedupe_hits(hits)
    if group_lines:
        hits = group_hits_into_lines(hits)
    hits = [h for h in hits if looks_like_useful_line(h)]
    hits = reread_large_titles(bgr_frame, hits, contrast)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return hits, elapsed_ms


def _normalize_title_candidate(text: str) -> str:
    """Join a split last letter: ``SIL O`` → ``SILO``. Ignore ``SIL OF``."""
    text = re.sub(r"\s+", " ", text).strip()
    parts = text.split(" ")
    if len(parts) == 2 and parts[1].isalpha() and len(parts[1]) == 1:
        return parts[0] + parts[1]
    return text


def _is_better_title(old: str, new: str, old_conf: float, new_conf: float) -> bool:
    """Prefer a one-letter extension of the same title (SIL → SILO)."""
    new = _normalize_title_candidate(new)
    old_key = re.sub(r"[^A-Za-z0-9]", "", old)
    new_key = re.sub(r"[^A-Za-z0-9]", "", new)
    if not new_key:
        return False
    if new_key.startswith(old_key) and len(new_key) == len(old_key) + 1:
        return True
    if new_key == old_key and new_conf > old_conf:
        return True
    return False


def reread_large_titles(bgr_frame, hits: list[OcrHit], contrast: bool) -> list[OcrHit]:
    """Re-OCR tall title boxes with extra room on the right.

    Full-frame Tesseract often clips the last letter of a stylized Apple TV
    wordmark. We crop that box, pad right, enlarge 2x, and read it as one line.
    """
    import cv2
    import pytesseract

    gray = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2GRAY)
    if contrast:
        gray = boost_contrast(gray)
    frame_h, frame_w = gray.shape[:2]
    updated: list[OcrHit] = []
    for hit in hits:
        if hit.height < TITLE_MIN_HEIGHT:
            updated.append(hit)
            continue
        pad = 12
        # First-pass boxes often already omit the last letter, so pad a lot.
        pad_right = max(200, int(hit.width * 0.7))
        x0 = max(0, hit.left - pad)
        y0 = max(0, hit.top - pad)
        x1 = min(frame_w, hit.left + hit.width + pad_right)
        y1 = min(frame_h, hit.top + hit.height + pad)
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            updated.append(hit)
            continue
        big = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        # Close small gaps in a ring letter so O is not read as C.
        _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
        # Full-frame grouping may already be "SIL OF". Start from the stem.
        stem = _normalize_title_candidate(hit.text)
        parts = stem.split()
        if (
            len(parts) >= 2
            and parts[0].isalpha()
            and len(parts[0]) >= 3
            and len(parts[1]) <= 2
        ):
            stem = parts[0]
        best_text = stem
        best_conf = hit.confidence
        for image in (big, cv2.bitwise_not(big), closed, cv2.bitwise_not(closed)):
            for psm in ("--psm 7", "--psm 8"):
                words = _hits_from_tesseract(image, min_conf=20, config=psm)
                if words:
                    words.sort(key=lambda w: w.left)
                    text = _normalize_title_candidate(_join_line_text([w.text for w in words]))
                    weight = sum(max(len(w.text), 1) for w in words)
                    conf = sum(w.confidence * max(len(w.text), 1) for w in words) / weight
                    if _is_better_title(best_text, text, best_conf, conf):
                        best_text = text
                        best_conf = conf
                raw = _normalize_title_candidate(
                    pytesseract.image_to_string(image, lang="eng", config=psm)
                )
                if raw and _is_better_title(best_text, raw, best_conf, best_conf):
                    best_text = raw
        updated.append(
            OcrHit(best_text, best_conf, hit.left, hit.top, hit.width, hit.height)
        )
    return updated


def looks_like_useful_line(hit: OcrHit) -> bool:
    """Drop leftover poster specks that have no real word or media token."""
    if re.search(r"[A-Za-z]{3,}", hit.text):
        return True
    if re.search(r"(?i)s\s*\d+|e\s*\d+|\d+\s*min|\d+\+|20\d{2}", hit.text):
        return True
    return False


def print_ocr_pass(hits: list[OcrHit], width: int, height: int, elapsed_ms: float) -> None:
    print("OCR PASS")
    print(f"Resolution: {width}x{height}")
    print()
    print("Detected:")
    if not hits:
        print("(no text found)")
    else:
        for hit in hits:
            print(
                f"“{hit.text}” — confidence {hit.confidence:.0f}% "
                f"— box ({hit.left}, {hit.top}, {hit.width}x{hit.height})"
            )
    print()
    print(f"OCR processing time: {elapsed_ms:.0f} ms")
    print()


def draw_ocr_overlay(frame, hits: list[OcrHit]):
    """Copy the frame and draw a box + label for each OCR hit.

    Drawing on a copy keeps the live capture and saved stills clean.
    """
    import cv2

    overlay = frame.copy()
    font = cv2.FONT_HERSHEY_SIMPLEX
    fh, fw = overlay.shape[:2]
    rx0, ry0, rx1, ry1 = top_right_rect(fw, fh)
    cv2.rectangle(overlay, (rx0, ry0), (rx1, ry1), (0, 180, 255), 1)
    cv2.putText(
        overlay,
        "top-right crop",
        (rx0 + 8, ry0 + 22),
        font,
        0.55,
        (0, 180, 255),
        1,
        cv2.LINE_AA,
    )
    for hit in hits:
        x1, y1 = hit.left, hit.top
        x2, y2 = hit.left + hit.width, hit.top + hit.height
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 220, 0), 2)

        label = f"{hit.text} {hit.confidence:.0f}%"
        if len(label) > 64:
            label = label[:61] + "..."
        (tw, th), baseline = cv2.getTextSize(label, font, 0.5, 1)
        label_y = y1 - 6 if y1 - th - 8 > 0 else y2 + th + 6
        cv2.rectangle(
            overlay,
            (x1, label_y - th - 4),
            (x1 + tw + 6, label_y + baseline),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            overlay,
            label,
            (x1 + 3, label_y - 2),
            font,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return overlay


def fit_preview(frame, max_w: int = PREVIEW_MAX_WIDTH, max_h: int = PREVIEW_MAX_HEIGHT):
    """Shrink the window if the capture is larger than the screen.

    This does not affect OCR — only what you see on the Mac display.
    """
    import cv2

    h, w = frame.shape[:2]
    scale = min(max_w / w, max_h / h, 1.0)
    if scale >= 0.999:
        return frame
    return cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


# ---------------------------------------------------------------------------
# Background OCR so a slow pass does not freeze the live preview
# ---------------------------------------------------------------------------
class OcrWorker:
    """One extra thread: take the newest frame, OCR it, store the results.

    The video loop keeps displaying frames. If OCR is still busy, we drop
    older queued frames and keep only the latest one.
    """

    def __init__(
        self,
        min_conf: float,
        group_lines: bool = True,
        invert: bool = True,
        contrast: bool = True,
    ) -> None:
        self._min_conf = min_conf
        self._group_lines = group_lines
        self._invert = invert
        self._contrast = contrast
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._hits: list[OcrHit] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ocr-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, frame) -> None:
        """Offer a frame for OCR. If the worker is busy, replace the waiting frame."""
        copied = frame.copy()
        try:
            self._queue.put_nowait(copied)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(copied)
            except queue.Full:
                pass

    def latest_hits(self) -> list[OcrHit]:
        with self._lock:
            return list(self._hits)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            hits, elapsed_ms = run_ocr(
                item, self._min_conf, self._group_lines, self._invert, self._contrast
            )
            height, width = item.shape[:2]
            print_ocr_pass(hits, width, height, elapsed_ms)
            with self._lock:
                self._hits = hits


# ---------------------------------------------------------------------------
# Save stills for later re-testing without the HDMI cable
# ---------------------------------------------------------------------------
def save_frame(frame) -> Path:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = SAVE_DIR / f"capture_{stamp}.png"
    import cv2

    if not cv2.imwrite(str(path), frame):
        print(f"Failed to save frame to {path}")
        return path
    print(f"Saved frame: {path}  ({frame.shape[1]}x{frame.shape[0]})")
    return path


# ---------------------------------------------------------------------------
# Modes: live capture, or OCR a saved still
# ---------------------------------------------------------------------------
def run_image_mode(
    image_path: Path,
    min_conf: float,
    group_lines: bool = True,
    invert: bool = True,
    contrast: bool = True,
) -> int:
    import cv2

    configure_tesseract()
    frame = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if frame is None:
        print(f"Could not read image: {image_path}")
        return 1
    hits, elapsed_ms = run_ocr(frame, min_conf, group_lines, invert, contrast)
    print_ocr_pass(hits, frame.shape[1], frame.shape[0], elapsed_ms)
    overlay = draw_ocr_overlay(frame, hits)
    cv2.imshow(WINDOW_NAME, fit_preview(overlay))
    print("Press Q in the window to close.")
    while True:
        key = cv2.waitKey(50) & 0xFF
        if key in (ord("q"), ord("Q"), 27):
            break
    cv2.destroyAllWindows()
    return 0


def run_live(args: argparse.Namespace) -> int:
    import cv2

    configure_tesseract()
    print()
    names = macos_camera_names()
    if names:
        print("macOS cameras:")
        for i, name in enumerate(names):
            print(f"  {i}: {name}")
        print()

    cap, error = open_capture(args.device, args.width, args.height, args.fps)
    if error:
        print(error)
        return 1

    worker = OcrWorker(
        min_conf=args.min_conf,
        group_lines=not args.raw_words,
        invert=not args.no_invert,
        contrast=not args.no_contrast,
    )
    worker.start()

    print("Live preview running.")
    print("  S  save current frame to tools/ocr_test_frames/")
    print("  Q  quit")
    if not args.no_contrast:
        print("  OCR uses a local contrast boost (CLAHE) on a grayscale copy.")
    if not args.no_invert:
        print("  OCR also reads an inverted copy so faded line-ends stay.")
    print("  Extra pass: top-right crop (Netflix title corner).")
    print()

    last_submit = 0.0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("Lost the video feed. Is the HDMI source still on?")
                break

            now = time.monotonic()
            if now - last_submit >= args.interval:
                worker.submit(frame)
                last_submit = now

            overlay = draw_ocr_overlay(frame, worker.latest_hits())
            cv2.imshow(WINDOW_NAME, fit_preview(overlay))

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (ord("s"), ord("S")):
                save_frame(frame)
    finally:
        worker.stop()
        cap.release()
        cv2.destroyAllWindows()
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="HDMI capture + Tesseract OCR diagnostic for Pigeon (standalone)."
    )
    parser.add_argument(
        "--device",
        type=int,
        default=DEFAULT_DEVICE_INDEX,
        help=f"OpenCV capture index (default {DEFAULT_DEVICE_INDEX}). Try 1 for a USB HDMI dongle.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List capture devices and exit.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=PREFERRED_WIDTH,
        help=f"Requested capture width (default {PREFERRED_WIDTH}).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=PREFERRED_HEIGHT,
        help=f"Requested capture height (default {PREFERRED_HEIGHT}).",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=PREFERRED_FPS,
        help=f"Requested capture frame rate (default {PREFERRED_FPS}).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=OCR_INTERVAL_SEC,
        help=f"Seconds between OCR passes (default {OCR_INTERVAL_SEC}).",
    )
    parser.add_argument(
        "--min-conf",
        type=float,
        default=DEFAULT_MIN_CONF,
        help=f"Hide OCR hits below this confidence 0–100 (default {DEFAULT_MIN_CONF}).",
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        help="OCR a saved PNG/JPG instead of opening the capture device.",
    )
    parser.add_argument(
        "--raw-words",
        action="store_true",
        help="Skip line grouping; print every Tesseract word (old behavior).",
    )
    parser.add_argument(
        "--no-invert",
        action="store_true",
        help="Skip the inverted pass; OCR the grayscale frame only.",
    )
    parser.add_argument(
        "--no-contrast",
        action="store_true",
        help="Skip the CLAHE contrast boost before OCR.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("OpenCV is not installed. Run:  python3 -m pip install opencv-python")
        return 1

    if args.list:
        print_device_list()
        return 0
    if args.image is not None:
        return run_image_mode(
            args.image.expanduser(),
            args.min_conf,
            not args.raw_words,
            not args.no_invert,
            not args.no_contrast,
        )
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
