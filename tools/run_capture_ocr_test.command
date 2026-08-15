#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "Pigeon HDMI capture + OCR test"
echo "------------------------------"
python3 tools/capture_ocr_test.py --list
echo
echo "Starting live preview."
echo "If the HDMI dongle is not listed above, plug it in and run this again."
echo "On a Mac, the dongle is often --device 1 (0 is usually FaceTime)."
echo

DEVICE="${1:-1}"
exec python3 tools/capture_ocr_test.py --device "$DEVICE"
